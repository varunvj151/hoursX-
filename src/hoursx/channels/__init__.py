"""Channels: two-way transports between agents and the outside world.

Import from here rather than from the adapter modules; the adapters are an
implementation detail and are loaded lazily by :func:`build_registry` so a
deployment never pays for a channel it has not configured.
"""

from hoursx.channels.base import (
    Channel,
    ChannelCredentials,
    ChannelError,
    ChannelKind,
    DeliveryResult,
    InboundMessage,
    OutboundMessage,
    SignatureError,
)
from hoursx.channels.dispatch import ChannelDispatcher
from hoursx.channels.router import (
    ChannelRegistry,
    ChannelRouter,
    ChannelTarget,
    RoutedRun,
    build_registry,
)

__all__ = [
    "Channel",
    "ChannelCredentials",
    "ChannelDispatcher",
    "ChannelError",
    "ChannelKind",
    "ChannelRegistry",
    "ChannelRouter",
    "ChannelTarget",
    "DeliveryResult",
    "InboundMessage",
    "OutboundMessage",
    "RoutedRun",
    "SignatureError",
    "build_registry",
]
