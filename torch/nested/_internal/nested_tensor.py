from typing import Tuple

import torch
from torch._C import DispatchKey, DispatchKeySet
from torch.fx.experimental.symbolic_shapes import has_free_symbols
from torch.utils.weak import WeakTensorKeyDictionary
from typing import *  # noqa: F403

_tensor_id_counter = 0
_tensor_symint_registry = WeakTensorKeyDictionary()


def get_tensor_symint(tensor, *, coeff=1):
    global _tensor_id_counter
    if tensor not in _tensor_symint_registry:
        _tensor_symint_registry[tensor] = torch._C._get_singleton_int(
            _tensor_id_counter, coeff
        )
        _tensor_id_counter += 1
    return _tensor_symint_registry[tensor]


class NestedTensor(torch.Tensor):
    _values: torch.Tensor  # type: ignore[assignment]
    _offsets: torch.Tensor
    # NOTE [ Singleton ints for ragged sizes and strides ]
    #
    # Jagged layout tensors are tensors that represent a n-dim tensor with a
    # ragged dimension, but are backed by an (n-1)-dim tensor underneath, e.g.,
    # a jagged tensor with outer shape [B, x, D] is represented internally by a
    # tensor with shape [sum(x), D] where we introduce what we call a singleton
    # (or skolem) denoted as "x" here (but sometimes denoted with "*" to
    # represent the ragged dimension, and sum(x) represents the dim of the inner
    # tensor or equivalently the sum of all the sizes of the constituent
    # tensors' varying lengths.
    #
    # We also use singleton ints to represent the strides of this tensor.
    # For example, a jagged tensor with shape [B, x, D] can be strided in two
    # ways: [xD, D, 1] and [x, 1, sum(x)], where xD represents x multiplied by D
    _size: Tuple[int, ...]
    _stride: Tuple[int, ...]
    # Indicates that the nth dimension is ragged
    _ragged_idx: int

    @staticmethod
    def __new__(
        cls,
        values,
        offsets,
        **kwargs,
    ):
        ks = DispatchKeySet(DispatchKey.NestedTensor)
        ks = ks.add(DispatchKey.AutogradNestedTensor)
        r = torch.Tensor._make_wrapper_subclass(  # type: ignore[attr-defined]
            cls,
            (0,),
            (0,),
            0,
            torch.contiguous_format,
            values.dtype,
            torch.jagged,
            values.device,
            False,
            kwargs.get("requires_grad", False),
            "sizes",
            False,
            True,  # dispatch_layout
            ks,
        )
        return r

    def __init__(self, values, offsets, **kwargs):
        super().__init__()
        # Only support jagged for now.
        assert offsets is not None
        assert offsets.ndim == 1
        assert not isinstance(values, NestedTensor)

        # Query cache for the symint associated with offsets (create a new one if needed).
        ragged_size = get_tensor_symint(offsets, coeff=1)
        B = offsets.shape[0] - 1
        Ds = values.shape[1:]
        self._size = (B, ragged_size, *Ds)
        stride = values.stride()
        self._strides = (ragged_size * stride[0], *stride)
        self._ragged_idx = 1
        self._values = values
        self._offsets = offsets

    def values(self):
        return DifferentiableValues.apply(self)

    def offsets(self):
        return self._offsets

    def __repr__(self):
        # We should implement this in torch/_tensor_str.py instead
        grad_fn_str = (
            f", requires_grad={self.requires_grad}" if self.requires_grad else ""
        )
        if self.grad_fn:
            grad_fn_str = f", grad_fn={self.grad_fn}"
        return f"NestedTensor(size={self._size}, offsets={self._offsets}{grad_fn_str})"

    def __reduce_ex__(self, proto):
        state = torch._utils._get_obj_state(self)

        # SymNodes are not serializable
        assert "_size" in state and "_strides" in state
        state = dict(state)
        del state["_size"]
        del state["_strides"]

        func = NestedTensor
        args = (self._values, self._offsets)
        return (torch._tensor._rebuild_from_type_v2, (func, type(self), args, state))

    def __tensor_flatten__(self):
        ctx = {
            "requires_grad": self.requires_grad,
            "ragged_size": self._size[self._ragged_idx],
        }
        return ["_values", "_offsets"], ctx

    @staticmethod
    def __tensor_unflatten__(inner_tensors: Dict, meta):
        assert len(inner_tensors) == 2
        values = inner_tensors["_values"]
        offsets = inner_tensors["_offsets"]

        # NOTE [ Storing symbolic values as plain attributes on subclasses ]
        #
        # When a subclass like NestedTensor stores a "size-like" value (which
        # can either be Symintified or not) into meta, it's responsible for:
        #
        #   (1) Propagating that symint during torch dispatch when performing
        #       operations, i.e. torch dispatch plays the role of a meta kernel.
        #
        #   (2) Facilitating the behavior around symbolic -> non-symbolic
        #       conversions and vice versa, see below.
        #       conversions and vice versa, see below.
        #
        # [ non-symbolic -> symbolic (fakification in meta_utils) ]
        #
        # __tensor_unflatten__ is passed symbolic dense tensors and meta from
        # non-symbolic subclasses. In this case, the subclass is responsible for
        # intercepting meta["ragged_size"] for example and replacing it with the
        # symintified version.
        #
        # [ symbolic -> non-symbolic ]
        #
        # __tensor_unflatten__ is passed non-symbolic dense tensors and with
        # meta extracted from fake subclasses. In this case the subclass gets
        # propagated the meta["ragged_size"] which is still a symint and the
        # subclass is responsible for making sure that the symint doesn't leak.
        #
        # Note that we cannot simply check if is_fake(values) because
        # during aot autograd, FunctionalTensors are not fake but hold
        # symbolic sizes.
        if has_free_symbols(offsets) or has_free_symbols(values):
            # Associate offsets (possibly fake, possibly functionalized) with the ragged_size.
            _tensor_symint_registry[offsets] = meta["ragged_size"]

        return NestedTensor(
            values,
            offsets=offsets,
            requires_grad=meta["requires_grad"],
        )

    @classmethod
    def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
        kwargs = {} if kwargs is None else kwargs

        # Lazy import to avoid circular dependency
        from .ops import lookup_jagged

        fn = lookup_jagged(func, *args, **kwargs)
        if fn is not None:
            return fn(*args, **kwargs)

        raise NotImplementedError(func)

    @classmethod
    def __torch_function__(cls, func, types, args=(), kwargs=None):
        if kwargs is None:
            kwargs = {}

        from .ops import jagged_torch_function

        try:
            return jagged_torch_function(func, *args, **kwargs)
        except NotImplementedError:
            pass
        with torch._C.DisableTorchFunctionSubclass():
            return func(*args, **kwargs)


# Returns nt.values() in a differentiable way
class DifferentiableValues(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x: NestedTensor):  # type: ignore[override]
        ctx.save_for_backward(x.offsets())
        return x._values

    @staticmethod
    def backward(ctx, gO: torch.Tensor):  # type: ignore[override]
        (offsets,) = ctx.saved_tensors
        return NestedTensor(gO, offsets=offsets)


# Need to make it obvious that users should be passing in offsets
def jagged_from_list(
    tensors: List[torch.Tensor],
    offsets: Optional[torch.Tensor],
    dtype=None,
    device=None,
) -> Tuple[NestedTensor, torch.Tensor]:
    """Constructs a NestedTensor backed by jagged layout from a list of tensors"""

    if not len(set(t.dtype for t in tensors)) == 1:  # noqa: C401
        raise RuntimeError(
            "When constructing a nested tensor, all tensors in list must have the same dtype"
        )
    if not len(set(t.device for t in tensors)) == 1:  # noqa: C401
        raise RuntimeError(
            "When constructing a nested tensor, all tensors in list must be on the same device"
        )

    # Check that the NT is representable by the jagged layout.
    # Jagged layout represents (B, *, D_0, D_1, ..., D_N), where the only
    # raggedness allowed is for the single dim immediately adjacent to the batch dim.
    sizes = [t.shape for t in tensors]
    non_first_sizes = [s[1:] for s in sizes]
    at_most_first_ragged = all(s == non_first_sizes[0] for s in non_first_sizes)
    if not at_most_first_ragged:
        raise RuntimeError(
            "Cannot represent given tensor list as a nested tensor with the jagged layout. "
            "Note that the jagged layout only represents shapes of the form "
            "(B, *, D_0, D_1, ..., D_N), with only * allowed to be ragged."
        )

    # Set properties appropriately.
    values = torch.cat(tensors, dim=0)
    to_kwargs = {}
    if device is not None:
        to_kwargs["device"] = device
    if dtype is not None:
        to_kwargs["dtype"] = dtype
    values = values.to(**to_kwargs)

    # Calculate jagged offsets if not provided.
    if offsets is None:
        # Jagged layout specifies that offsets are stored as int64 on the same device as values.
        offsets = torch.cat(
            [
                torch.zeros(1, dtype=torch.int64, device=values.device),
                torch.tensor([s[0] for s in sizes], device=values.device).cumsum(dim=0),
            ]
        )

    return (
        nested_view_from_values_offsets(values, offsets),
        offsets,
    )  # type: ignore[return-value]


# NB: A dummy arg is required so that NestedTensor.__torch_dispatch__() is invoked
# for _nested_view_from_values_offsets(). Sizes don't matter here, so they're kept simple.
# This arg is otherwise unused.
_nt_view_dummy = NestedTensor(
    values=torch.randn(1, 1, device="meta"), offsets=torch.randn(1, device="meta")
)


def nested_view_from_values_offsets(values, offsets):
    return torch._nested_view_from_values_offsets(values, offsets, _nt_view_dummy)
