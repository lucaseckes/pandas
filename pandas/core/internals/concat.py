from __future__ import annotations

import itertools
from typing import (
    TYPE_CHECKING,
    Sequence,
)

import numpy as np

from pandas._libs import (
    NaT,
    internals as libinternals,
    lib,
)
from pandas._libs.missing import NA
from pandas.util._decorators import cache_readonly

from pandas.core.dtypes.astype import astype_array
from pandas.core.dtypes.cast import (
    ensure_dtype_can_hold_na,
    find_common_type,
)
from pandas.core.dtypes.common import (
    is_1d_only_ea_dtype,
    is_dtype_equal,
    is_scalar,
    needs_i8_conversion,
)
from pandas.core.dtypes.concat import concat_compat
from pandas.core.dtypes.dtypes import ExtensionDtype
from pandas.core.dtypes.missing import (
    is_valid_na_for_dtype,
    isna,
    isna_all,
)

from pandas.core.arrays import ExtensionArray
from pandas.core.arrays.sparse import SparseDtype
from pandas.core.construction import ensure_wrapped_if_datetimelike
from pandas.core.internals.array_manager import (
    ArrayManager,
    NullArrayProxy,
)
from pandas.core.internals.blocks import (
    ensure_block_shape,
    new_block_2d,
)
from pandas.core.internals.managers import (
    BlockManager,
    make_na_array,
)

if TYPE_CHECKING:
    from pandas._typing import (
        ArrayLike,
        AxisInt,
        DtypeObj,
        Manager,
    )

    from pandas import Index
    from pandas.core.internals.blocks import (
        Block,
        BlockPlacement,
    )


def _concatenate_array_managers(
    mgrs_indexers, axes: list[Index], concat_axis: AxisInt, copy: bool
) -> Manager:
    """
    Concatenate array managers into one.

    Parameters
    ----------
    mgrs_indexers : list of (ArrayManager, {axis: indexer,...}) tuples
    axes : list of Index
    concat_axis : int
    copy : bool

    Returns
    -------
    ArrayManager
    """
    # reindex all arrays
    mgrs = []
    for mgr, indexers in mgrs_indexers:
        axis1_made_copy = False
        for ax, indexer in indexers.items():
            mgr = mgr.reindex_indexer(
                axes[ax], indexer, axis=ax, allow_dups=True, use_na_proxy=True
            )
            if ax == 1 and indexer is not None:
                axis1_made_copy = True
        if copy and concat_axis == 0 and not axis1_made_copy:
            # for concat_axis 1 we will always get a copy through concat_arrays
            mgr = mgr.copy()
        mgrs.append(mgr)

    if concat_axis == 1:
        # concatting along the rows -> concat the reindexed arrays
        # TODO(ArrayManager) doesn't yet preserve the correct dtype
        arrays = [
            concat_arrays([mgrs[i].arrays[j] for i in range(len(mgrs))])
            for j in range(len(mgrs[0].arrays))
        ]
    else:
        # concatting along the columns -> combine reindexed arrays in a single manager
        assert concat_axis == 0
        arrays = list(itertools.chain.from_iterable([mgr.arrays for mgr in mgrs]))

    new_mgr = ArrayManager(arrays, [axes[1], axes[0]], verify_integrity=False)
    return new_mgr


def concat_arrays(to_concat: list) -> ArrayLike:
    """
    Alternative for concat_compat but specialized for use in the ArrayManager.

    Differences: only deals with 1D arrays (no axis keyword), assumes
    ensure_wrapped_if_datetimelike and does not skip empty arrays to determine
    the dtype.
    In addition ensures that all NullArrayProxies get replaced with actual
    arrays.

    Parameters
    ----------
    to_concat : list of arrays

    Returns
    -------
    np.ndarray or ExtensionArray
    """
    # ignore the all-NA proxies to determine the resulting dtype
    to_concat_no_proxy = [x for x in to_concat if not isinstance(x, NullArrayProxy)]

    dtypes = {x.dtype for x in to_concat_no_proxy}
    single_dtype = len(dtypes) == 1

    if single_dtype:
        target_dtype = to_concat_no_proxy[0].dtype
    elif all(x.kind in "iub" and isinstance(x, np.dtype) for x in dtypes):
        # GH#42092
        target_dtype = np.find_common_type(list(dtypes), [])
    else:
        target_dtype = find_common_type([arr.dtype for arr in to_concat_no_proxy])

    to_concat = [
        arr.to_array(target_dtype)
        if isinstance(arr, NullArrayProxy)
        else astype_array(arr, target_dtype, copy=False)
        for arr in to_concat
    ]

    if isinstance(to_concat[0], ExtensionArray):
        cls = type(to_concat[0])
        return cls._concat_same_type(to_concat)

    result = np.concatenate(to_concat)

    # TODO decide on exact behaviour (we shouldn't do this only for empty result)
    # see https://github.com/pandas-dev/pandas/issues/39817
    if len(result) == 0:
        # all empties -> check for bool to not coerce to float
        kinds = {obj.dtype.kind for obj in to_concat_no_proxy}
        if len(kinds) != 1:
            if "b" in kinds:
                result = result.astype(object)
    return result


def concatenate_managers(
    mgrs_indexers, axes: list[Index], concat_axis: AxisInt, copy: bool
) -> Manager:
    """
    Concatenate block managers into one.

    Parameters
    ----------
    mgrs_indexers : list of (BlockManager, {axis: indexer,...}) tuples
    axes : list of Index
    concat_axis : int
    copy : bool

    Returns
    -------
    BlockManager
    """
    # TODO(ArrayManager) this assumes that all managers are of the same type
    if isinstance(mgrs_indexers[0][0], ArrayManager):
        return _concatenate_array_managers(mgrs_indexers, axes, concat_axis, copy)

    # Assertions disabled for performance
    # for tup in mgrs_indexers:
    #    # caller is responsible for ensuring this
    #    indexers = tup[1]
    #    assert concat_axis not in indexers

    if concat_axis == 0:
        return _concat_managers_axis0(mgrs_indexers, axes, copy)

    mgrs_indexers = _maybe_reindex_columns_na_proxy(axes, mgrs_indexers)

    concat_plan = _get_combined_plan([mgr for mgr, _ in mgrs_indexers])

    blocks = []

    for placement, join_units in concat_plan:
        unit = join_units[0]
        blk = unit.block

        if len(join_units) == 1:
            values = blk.values
            if copy:
                values = values.copy()
            else:
                values = values.view()
            fastpath = True
        elif _is_uniform_join_units(join_units):
            vals = [ju.block.values for ju in join_units]

            if not blk.is_extension:
                # _is_uniform_join_units ensures a single dtype, so
                #  we can use np.concatenate, which is more performant
                #  than concat_compat
                # error: Argument 1 to "concatenate" has incompatible type
                # "List[Union[ndarray[Any, Any], ExtensionArray]]";
                # expected "Union[_SupportsArray[dtype[Any]],
                # _NestedSequence[_SupportsArray[dtype[Any]]]]"
                values = np.concatenate(vals, axis=1)  # type: ignore[arg-type]
            elif is_1d_only_ea_dtype(blk.dtype):
                # TODO(EA2D): special-casing not needed with 2D EAs
                values = concat_compat(vals, axis=1, ea_compat_axis=True)
                values = ensure_block_shape(values, ndim=2)
            else:
                values = concat_compat(vals, axis=1)

            values = ensure_wrapped_if_datetimelike(values)

            fastpath = blk.values.dtype == values.dtype
        else:
            values = _concatenate_join_units(join_units, copy=copy)
            fastpath = False

        if fastpath:
            b = blk.make_block_same_class(values, placement=placement)
        else:
            b = new_block_2d(values, placement=placement)

        blocks.append(b)

    return BlockManager(tuple(blocks), axes)


def _concat_managers_axis0(
    mgrs_indexers, axes: list[Index], copy: bool
) -> BlockManager:
    """
    concat_managers specialized to concat_axis=0, with reindexing already
    having been done in _maybe_reindex_columns_na_proxy.
    """
    had_reindexers = {
        i: len(mgrs_indexers[i][1]) > 0 for i in range(len(mgrs_indexers))
    }
    mgrs_indexers = _maybe_reindex_columns_na_proxy(axes, mgrs_indexers)

    mgrs: list[BlockManager] = [x[0] for x in mgrs_indexers]

    offset = 0
    blocks: list[Block] = []
    for i, mgr in enumerate(mgrs):
        # If we already reindexed, then we definitely don't need another copy
        made_copy = had_reindexers[i]

        for blk in mgr.blocks:
            if made_copy:
                nb = blk.copy(deep=False)
            elif copy:
                nb = blk.copy()
            else:
                # by slicing instead of copy(deep=False), we get a new array
                #  object, see test_concat_copy
                nb = blk.getitem_block(slice(None))
            nb._mgr_locs = nb._mgr_locs.add(offset)
            blocks.append(nb)

        offset += len(mgr.items)

    result = BlockManager(tuple(blocks), axes)
    return result


def _maybe_reindex_columns_na_proxy(
    axes: list[Index], mgrs_indexers: list[tuple[BlockManager, dict[int, np.ndarray]]]
) -> list[tuple[BlockManager, dict[int, np.ndarray]]]:
    """
    Reindex along columns so that all of the BlockManagers being concatenated
    have matching columns.

    Columns added in this reindexing have dtype=np.void, indicating they
    should be ignored when choosing a column's final dtype.
    """
    new_mgrs_indexers: list[tuple[BlockManager, dict[int, np.ndarray]]] = []

    for mgr, indexers in mgrs_indexers:
        # For axis=0 (i.e. columns) we use_na_proxy and only_slice, so this
        #  is a cheap reindexing.
        for i, indexer in indexers.items():
            mgr = mgr.reindex_indexer(
                axes[i],
                indexers[i],
                axis=i,
                copy=False,
                only_slice=True,  # only relevant for i==0
                allow_dups=True,
                use_na_proxy=True,  # only relevant for i==0
            )
        new_mgrs_indexers.append((mgr, {}))
    return new_mgrs_indexers


def _get_combined_plan(
    mgrs: list[BlockManager],
) -> list[tuple[BlockPlacement, list[JoinUnit]]]:
    plan = []

    max_len = mgrs[0].shape[0]

    blknos_list = [mgr.blknos for mgr in mgrs]
    pairs = libinternals.get_concat_blkno_indexers(blknos_list)
    for ind, (blknos, bp) in enumerate(pairs):
        # assert bp.is_slice_like
        # assert len(bp) > 0

        units_for_bp = []
        for k, mgr in enumerate(mgrs):
            blkno = blknos[k]

            nb = _get_block_for_concat_plan(mgr, bp, blkno, max_len=max_len)
            unit = JoinUnit(nb)
            units_for_bp.append(unit)

        plan.append((bp, units_for_bp))

    return plan


def _get_block_for_concat_plan(
    mgr: BlockManager, bp: BlockPlacement, blkno: int, *, max_len: int
) -> Block:
    blk = mgr.blocks[blkno]
    # Assertions disabled for performance:
    #  assert bp.is_slice_like
    #  assert blkno != -1
    #  assert (mgr.blknos[bp] == blkno).all()

    if len(bp) == len(blk.mgr_locs) and (
        blk.mgr_locs.is_slice_like and blk.mgr_locs.as_slice.step == 1
    ):
        nb = blk
    else:
        ax0_blk_indexer = mgr.blklocs[bp.indexer]

        slc = lib.maybe_indices_to_slice(ax0_blk_indexer, max_len)
        # TODO: in all extant test cases 2023-04-08 we have a slice here.
        #  Will this always be the case?
        nb = blk.getitem_block(slc)

    # assert nb.shape == (len(bp), mgr.shape[1])
    return nb


class JoinUnit:
    def __init__(self, block: Block) -> None:
        self.block = block

    def __repr__(self) -> str:
        return f"{type(self).__name__}({repr(self.block)})"

    def _is_valid_na_for(self, dtype: DtypeObj) -> bool:
        """
        Check that we are all-NA of a type/dtype that is compatible with this dtype.
        Augments `self.is_na` with an additional check of the type of NA values.
        """
        if not self.is_na:
            return False

        blk = self.block
        if blk.dtype.kind == "V":
            return True

        if blk.dtype == object:
            values = blk.values
            return all(is_valid_na_for_dtype(x, dtype) for x in values.ravel(order="K"))

        na_value = blk.fill_value
        if na_value is NaT and not is_dtype_equal(blk.dtype, dtype):
            # e.g. we are dt64 and other is td64
            # fill_values match but we should not cast blk.values to dtype
            # TODO: this will need updating if we ever have non-nano dt64/td64
            return False

        if na_value is NA and needs_i8_conversion(dtype):
            # FIXME: kludge; test_append_empty_frame_with_timedelta64ns_nat
            #  e.g. blk.dtype == "Int64" and dtype is td64, we dont want
            #  to consider these as matching
            return False

        # TODO: better to use can_hold_element?
        return is_valid_na_for_dtype(na_value, dtype)

    @cache_readonly
    def is_na(self) -> bool:
        blk = self.block
        if blk.dtype.kind == "V":
            return True

        if not blk._can_hold_na:
            return False

        values = blk.values
        if values.size == 0:
            return True
        if isinstance(values.dtype, SparseDtype):
            return False

        if values.ndim == 1:
            # TODO(EA2D): no need for special case with 2D EAs
            val = values[0]
            if not is_scalar(val) or not isna(val):
                # ideally isna_all would do this short-circuiting
                return False
            return isna_all(values)
        else:
            val = values[0][0]
            if not is_scalar(val) or not isna(val):
                # ideally isna_all would do this short-circuiting
                return False
            return all(isna_all(row) for row in values)

    def get_reindexed_values(self, empty_dtype: DtypeObj, upcasted_na) -> ArrayLike:
        values: ArrayLike

        if upcasted_na is None and self.block.dtype.kind != "V":
            # No upcasting is necessary
            fill_value = self.block.fill_value
            values = self.block.values
        else:
            fill_value = upcasted_na

            if self._is_valid_na_for(empty_dtype):
                # note: always holds when self.block.dtype.kind == "V"
                blk_dtype = self.block.dtype

                if blk_dtype == np.dtype("object"):
                    # we want to avoid filling with np.nan if we are
                    # using None; we already know that we are all
                    # nulls
                    values = self.block.values.ravel(order="K")
                    if len(values) and values[0] is None:
                        fill_value = None

                return make_na_array(empty_dtype, self.block.shape, fill_value)

            if not self.block._can_consolidate:
                # preserve these for validation in concat_compat
                return self.block.values

            if self.block.is_bool:
                # External code requested filling/upcasting, bool values must
                # be upcasted to object to avoid being upcasted to numeric.
                values = self.block.astype(np.dtype("object")).values
            else:
                # No dtype upcasting is done here, it will be performed during
                # concatenation itself.
                values = self.block.values

        # If there's no indexing to be done, we want to signal outside
        # code that this array must be copied explicitly.  This is done
        # by returning a view and checking `retval.base`.
        values = values.view()
        return values


def _concatenate_join_units(join_units: list[JoinUnit], copy: bool) -> ArrayLike:
    """
    Concatenate values from several join units along axis=1.
    """
    empty_dtype = _get_empty_dtype(join_units)

    has_none_blocks = any(unit.block.dtype.kind == "V" for unit in join_units)
    upcasted_na = _dtype_to_na_value(empty_dtype, has_none_blocks)

    to_concat = [
        ju.get_reindexed_values(empty_dtype=empty_dtype, upcasted_na=upcasted_na)
        for ju in join_units
    ]

    if len(to_concat) == 1:
        # Only one block, nothing to concatenate.
        concat_values = to_concat[0]
        if copy:
            if isinstance(concat_values, np.ndarray):
                # non-reindexed (=not yet copied) arrays are made into a view
                # in JoinUnit.get_reindexed_values
                if concat_values.base is not None:
                    concat_values = concat_values.copy()
            else:
                concat_values = concat_values.copy()

    elif any(is_1d_only_ea_dtype(t.dtype) for t in to_concat):
        # TODO(EA2D): special case not needed if all EAs used HybridBlocks

        # error: No overload variant of "__getitem__" of "ExtensionArray" matches
        # argument type "Tuple[int, slice]"
        to_concat = [
            t
            if is_1d_only_ea_dtype(t.dtype)
            else t[0, :]  # type: ignore[call-overload]
            for t in to_concat
        ]
        concat_values = concat_compat(to_concat, axis=0, ea_compat_axis=True)
        concat_values = ensure_block_shape(concat_values, 2)

    else:
        concat_values = concat_compat(to_concat, axis=1)

    return concat_values


def _dtype_to_na_value(dtype: DtypeObj, has_none_blocks: bool):
    """
    Find the NA value to go with this dtype.
    """
    if isinstance(dtype, ExtensionDtype):
        return dtype.na_value
    elif dtype.kind in "mM":
        return dtype.type("NaT")
    elif dtype.kind in "fc":
        return dtype.type("NaN")
    elif dtype.kind == "b":
        # different from missing.na_value_for_dtype
        return None
    elif dtype.kind in "iu":
        if not has_none_blocks:
            # different from missing.na_value_for_dtype
            return None
        return np.nan
    elif dtype.kind == "O":
        return np.nan
    raise NotImplementedError


def _get_empty_dtype(join_units: Sequence[JoinUnit]) -> DtypeObj:
    """
    Return dtype and N/A values to use when concatenating specified units.

    Returned N/A value may be None which means there was no casting involved.

    Returns
    -------
    dtype
    """
    if len(join_units) == 1:
        blk = join_units[0].block
        return blk.dtype

    if lib.dtypes_all_equal([ju.block.dtype for ju in join_units]):
        empty_dtype = join_units[0].block.dtype
        return empty_dtype

    has_none_blocks = any(unit.block.dtype.kind == "V" for unit in join_units)

    dtypes = [unit.block.dtype for unit in join_units if not unit.is_na]
    if not len(dtypes):
        dtypes = [
            unit.block.dtype for unit in join_units if unit.block.dtype.kind != "V"
        ]

    dtype = find_common_type(dtypes)
    if has_none_blocks:
        dtype = ensure_dtype_can_hold_na(dtype)
    return dtype


def _is_uniform_join_units(join_units: list[JoinUnit]) -> bool:
    """
    Check if the join units consist of blocks of uniform type that can
    be concatenated using Block.concat_same_type instead of the generic
    _concatenate_join_units (which uses `concat_compat`).

    """
    first = join_units[0].block
    if first.dtype.kind == "V":
        return False
    return (
        # exclude cases where a) ju.block is None or b) we have e.g. Int64+int64
        all(type(ju.block) is type(first) for ju in join_units)
        and
        # e.g. DatetimeLikeBlock can be dt64 or td64, but these are not uniform
        all(
            is_dtype_equal(ju.block.dtype, first.dtype)
            # GH#42092 we only want the dtype_equal check for non-numeric blocks
            #  (for now, may change but that would need a deprecation)
            or ju.block.dtype.kind in "iub"
            for ju in join_units
        )
        and
        # no blocks that would get missing values (can lead to type upcasts)
        # unless we're an extension dtype.
        all(not ju.is_na or ju.block.is_extension for ju in join_units)
        and
        # only use this path when there is something to concatenate
        len(join_units) > 1
    )
