"""Apps the lab knows how to deploy, and the service names chaos can target.

Service names are the ground-truth component (after the company prefix).
They match the demo's own names: Online Boutique and Sock Shop are the
RCAEval apps; the OpenTelemetry demo uses its shorter component names.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    name: str
    http_port: int | None = None


@dataclass(frozen=True)
class App:
    key: str
    services: tuple[Service, ...]
    load_deployment_suffix: str
    tracing: str  # boutique | otel | none

    def names(self) -> list[str]:
        return [service.name for service in self.services]

    def http_port(self, name: str) -> int | None:
        for service in self.services:
            if service.name == name:
                return service.http_port
        return None


ONLINE_BOUTIQUE = App(
    key="online-boutique",
    tracing="boutique",
    load_deployment_suffix="loadgenerator",
    services=(
        Service("frontend", 8080),
        Service("checkoutservice"),
        Service("cartservice"),
        Service("productcatalogservice"),
        Service("currencyservice"),
        Service("paymentservice"),
        Service("shippingservice"),
        Service("emailservice"),
        Service("recommendationservice"),
        Service("adservice"),
        Service("redis-cart"),
    ),
)

# Reduced set: the chart values turn the rest off so one laptop can run it.
OTEL_DEMO = App(
    key="otel-demo",
    tracing="otel",
    load_deployment_suffix="load-generator",
    services=(
        Service("frontend", 8080),
        Service("checkout", 8080),
        Service("cart"),
        Service("currency"),
        Service("payment"),
        Service("shipping"),
        Service("email"),
        Service("product-catalog"),
        Service("recommendation"),
        Service("ad"),
        Service("quote"),
    ),
)

SOCK_SHOP = App(
    key="sock-shop",
    tracing="none",
    load_deployment_suffix="loadgenerator",
    services=(
        Service("front-end", 80),
        Service("catalogue"),
        Service("carts"),
        Service("orders"),
        Service("payment"),
        Service("shipping"),
        Service("user"),
        Service("queue-master"),
    ),
)

APPS: dict[str, App] = {
    ONLINE_BOUTIQUE.key: ONLINE_BOUTIQUE,
    OTEL_DEMO.key: OTEL_DEMO,
    SOCK_SHOP.key: SOCK_SHOP,
}

# Components the OpenTelemetry demo chart can leave out. Accounting and fraud
# are the Kafka consumers. Checkout stays on; plan.otel_demo_values clears its
# Kafka wait and address so the process can start.
OTEL_DISABLED = (
    "accounting",
    "fraud-detection",
    "kafka",
    "chatbot",
    "mcp",
    "image-provider",
    "telemetry-docs",
    "opamp-server",
    "firepit",
    "agent",
)
