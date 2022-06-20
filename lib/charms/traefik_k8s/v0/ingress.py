# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

r"""# Interface Library for ingress.

This library wraps relation endpoints using the `ingress` interface
and provides a Python API for both requesting and providing per-application
ingress, with load-balancing occurring across all units.

## Getting Started

To get started using the library, you just need to fetch the library using `charmcraft`.

```shell
cd some-charm
charmcraft fetch-lib charms.traefik_k8s.v0.ingress
```

In the `metadata.yaml` of the charm, add the following:

```yaml
requires:
    ingress:
        interface: ingress
        limit: 1
```

Then, to initialise the library:

```python
# ...
from charms.traefik_k8s.v0.ingress import IngressPerAppRequirer

class SomeCharm(CharmBase):
  def __init__(self, *args):
    # ...
    self.ingress = IngressPerAppRequirer(self, port=80)
    # The following event is triggered when the ingress URL to be used
    # by this deployment of the `SomeCharm` changes or there is no longer
    # an ingress URL available, that is, `self.ingress` would
    # return `None`.
    self.framework.observe(
        self.ingress.on.ready, self._handle_ingress
    )
    # ...

    def _handle_ingress(self, event):
        logger.info("This app's ingress URL: %s", self.ingress.url)
```
"""

import logging
import socket
from typing import Optional, Any

import yaml
from ops.charm import CharmBase, RelationEvent
from ops.framework import EventSource, Object, ObjectEvents, StoredState
from ops.model import Relation

# The unique Charmhub library identifier, never change it
LIBID = "e6de2a5cd5b34422a204668f3b8f90d2"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 7

DEFAULT_RELATION_NAME = "ingress"
RELATION_INTERFACE = "ingress"

log = logging.getLogger(__name__)

try:
    import pydantic
except ModuleNotFoundError as e:
    log.error('you need to `pip install pydantic` and add '
              'pydantic to the requirements.txt for this charm.')
    raise e


class ProviderIngressData(pydantic.BaseModel):
    url = pydantic.Field()  # type: str


class ProviderData(pydantic.BaseModel):
    ingress = pydantic.Field()  # type: pydantic.Json[ProviderIngressData]


class RequirerData(pydantic.BaseModel):
    model = pydantic.Field()  # type: str
    name = pydantic.Field()  # type: str
    host = pydantic.Field()  # type: str
    port = pydantic.Field()  # type: int

    # you can do cool things:
    @pydantic.validator('model', 'name', 'host')
    def name_cannot_contain_spaces(cls, v):
        if ' ' in v:
            raise ValueError('cannot contain spaces')


class _IngressPerAppBase(Object):
    """Base class for IngressPerUnit interface classes."""

    def __init__(self, charm: CharmBase, relation_name: str = DEFAULT_RELATION_NAME):
        super().__init__(charm, relation_name)

        self.charm: CharmBase = charm
        self.relation_name = relation_name
        self.app = self.charm.app
        self.unit = self.charm.unit

        observe = self.framework.observe
        rel_events = charm.on[relation_name]
        observe(rel_events.relation_created, self._handle_relation)
        observe(rel_events.relation_joined, self._handle_relation)
        observe(rel_events.relation_changed, self._handle_relation)
        observe(rel_events.relation_broken, self._handle_relation_broken)
        observe(charm.on.leader_elected, self._handle_upgrade_or_leader)
        observe(charm.on.upgrade_charm, self._handle_upgrade_or_leader)

    @property
    def relations(self):
        """The list of Relation instances associated with this endpoint."""
        return list(self.charm.model.relations[self.relation_name])

    def _handle_relation(self, event):
        """Subclasses should implement this method to handle a relation update."""
        pass

    def _handle_relation_broken(self, event):
        """Subclasses should implement this method to handle a relation breaking."""
        pass

    def _handle_upgrade_or_leader(self, event):
        """Subclasses should implement this method to handle upgrades or leadership change."""
        pass


class IngressPerAppDataProvidedEvent(RelationEvent):
    """Event representing that ingress data has been provided for an app."""


class IngressPerAppDataRemovedEvent(RelationEvent):
    """Event representing that ingress data has been removed for an app."""


class IngressPerAppProviderEvents(ObjectEvents):
    """Container for IPA Provider events."""

    data_provided = EventSource(IngressPerAppDataProvidedEvent)
    data_removed = EventSource(IngressPerAppDataRemovedEvent)


class IngressPerAppProvider(_IngressPerAppBase):
    """Implementation of the provider of ingress."""

    on = IngressPerAppProviderEvents()

    def __init__(self, charm: CharmBase, relation_name: str = DEFAULT_RELATION_NAME):
        """Constructor for IngressPerAppProvider.

        Args:
            charm: The charm that is instantiating the instance.
            relation_name: The name of the relation endpoint to bind to
                (defaults to "ingress").
        """
        super().__init__(charm, relation_name)
        self.framework.observe(
            self.charm.on[relation_name].relation_joined, self._share_version_info
        )

    def _share_version_info(self, event):
        """Backwards-compatibility shim for version negotiation.

        Allows older versions of IPA (requirer side) to interact with this
        provider without breaking.
        Will be removed in a future version of this library.
        Do not use.
        """
        relation = event.relation
        if self.charm.unit.is_leader():
            log.info("shared supported_versions shim information")
            relation.data[self.charm.app]["_supported_versions"] = "- v1"

    def _handle_relation(self, event):
        # created, joined or changed: if remote side has sent the required data:
        # notify listeners.
        if self.is_ready(event.relation):
            self.on.data_provided.emit(event.relation)

    def _handle_relation_broken(self, event):
        self.on.data_removed.emit(event.relation)

    def wipe_ingress_data(self, relation: Relation):
        """Clear ingress data from relation."""
        del relation.data[self.app]["data"]

    def _get_requirer_data(self, relation: Relation) -> RequirerData:
        """Fetch and validate the requirer's app databag."""
        if not relation.app or not relation.app.name:
            # Handle edge case where remote app name can be missing, e.g.,
            # relation_broken events.
            # FIXME https://github.com/canonical/traefik-k8s-operator/issues/34
            return {}

        remote_data = relation.data[relation.app].get("data")
        if not remote_data:
            return {}

        remote_deserialized = RequirerData.parse_obj(remote_data)
        return remote_deserialized

    def get_data(self, relation: Relation) -> RequirerData:
        """Fetch the remote app's databag, i.e. the requirer data."""
        return self._get_requirer_data(relation)

    def is_ready(self, relation: Relation = None):
        """The Provider is ready if the requirer has sent valid data."""
        if not relation:
            return any(map(self.is_ready, self.relations))

        try:
            return bool(self._get_requirer_data(relation))
        except pydantic.ValidationError as e:
            log.warning("Requirer not ready; validation error encountered: %s" % str(e))
            return False

    def _provider_app_data(self, relation: Relation) -> Optional[ProviderIngressData]:
        """Fetch and validate this provider's app databag."""
        if not relation.app or not relation.app.name:
            # Handle edge case where remote app name can be missing, e.g.,
            # relation_broken events.
            # FIXME https://github.com/canonical/traefik-k8s-operator/issues/34
            return {}  # noqa

        # we start by looking at the provider's app databag
        if self.unit.is_leader():
            # only leaders can read their app's data
            data = ProviderData.parse_obj(relation.data[self.app]['data'])
            return data.ingress
        return None

    def publish_url(self, relation: Relation, url: str):
        """Publish to the app databag the ingress url."""
        ingress = ProviderIngressData(url=url)
        relation.data[self.app]["data"] = ProviderData(ingress=ingress)

    @property
    def proxied_endpoints(self):
        """Returns the ingress settings provided to applications by this IngressPerAppProvider.

        For example, when this IngressPerAppProvider has provided the
        `http://foo.bar/my-model.my-app` URL to the my-app application, the returned dictionary
        will be:

        ```
        {
            "my-app": {
                "url": "http://foo.bar/my-model.my-app"
            }
        }
        ```
        """
        results = {}

        for ingress_relation in self.relations:
            provider_app_data = self._provider_app_data(ingress_relation)
            results[ingress_relation.app.name] = provider_app_data

        return results


class IngressPerAppReadyEvent(RelationEvent):
    """Event representing that ingress for an app is ready."""


class IngressPerAppRevokedEvent(RelationEvent):
    """Event representing that ingress for an app has been revoked."""


class IngressPerAppRequirerEvents(ObjectEvents):
    """Container for IPA Requirer events."""

    ready = EventSource(IngressPerAppReadyEvent)
    revoked = EventSource(IngressPerAppRevokedEvent)


class IngressPerAppRequirer(_IngressPerAppBase):
    """Implementation of the requirer of the ingress relation."""

    on = IngressPerAppRequirerEvents()
    # used to prevent spurious urls to be sent out if the event we're currently
    # handling is a relation-broken one.
    _stored = StoredState()

    def __init__(
        self,
        charm: CharmBase,
        relation_name: str = DEFAULT_RELATION_NAME,
        *,
        host: str = None,
        port: int = None,
    ):
        """Constructor for IngressRequirer.

        The request args can be used to specify the ingress properties when the
        instance is created. If any are set, at least `port` is required, and
        they will be sent to the ingress provider as soon as it is available.
        All request args must be given as keyword args.

        Args:
            charm: the charm that is instantiating the library.
            relation_name: the name of the relation endpoint to bind to (defaults to `ingress`);
                relation must be of interface type `ingress` and have "limit: 1")
            host: Hostname to be used by the ingress provider to address the requiring
                application; if unspecified, the default Kubernetes service name will be used.

        Request Args:
            port: the port of the service
        """
        super().__init__(charm, relation_name)
        self.charm: CharmBase = charm
        self.relation_name = relation_name

        self._stored.set_default(current_url=None)

        # if instantiated with a port, and we are related, then
        # we immediately publish our ingress data  to speed up the process.
        if port:
            self._auto_data = host, port
        else:
            self._auto_data = None

    def _handle_relation(self, event):
        # created, joined or changed: if we have auto data: publish it
        self._publish_auto_data(event.relation)

        if self.is_ready():
            self._emit_ingress_ready_event(event)

    def _handle_relation_broken(self, event):
        self.on.revoked.emit(event.relation)

    def _handle_upgrade_or_leader(self, event):
        """On upgrade/leadership change: ensure we publish the data we have."""
        for relation in self.relations:
            self._publish_auto_data(relation)

    def is_ready(self):
        """The Requirer is ready if the Provider has sent valid data."""
        try:
            return bool(self.url)
        except pydantic.ValidationError as e:
            log.warning("Requirer not ready; validation error encountered: %s" % str(e))
            return False

    def _emit_ingress_ready_event(self, event):
        # Avoid spurious events, emit only when there is a NEW URL available
        new_url = self.url
        if self._stored.current_url != new_url:
            self._stored.current_url = new_url
            self.on.ready.emit(event.relation)

    def _publish_auto_data(self, relation: Relation):
        if self._auto_data and self.unit.is_leader():
            host, port = self._auto_data
            self.provide_ingress_requirements(host=host, port=port)

    def provide_ingress_requirements(self, *, host: str = None, port: int):
        """Publishes the data that Traefik needs to provide ingress.

        NB only the leader unit is supposed to do this.

        Args:
            host: Hostname to be used by the ingress provider to address the
             requirer unit; if unspecified, the pod ip of the unit will be used
             instead
            port: the port of the service (required)
        """
        # get only the leader to publish the data since we only
        # require one unit to publish it -- it will not differ between units,
        # unlike in ingress-per-unit.
        assert self.unit.is_leader(), "only leaders should do this."

        if not host:
            host = socket.getfqdn()

        data = RequirerData(model=self.model.name, name=self.app.name,
                            host=host, port=port)
        self.relation.data[self.app]["data"] = data.json()

    @property
    def relation(self):
        """The established Relation instance, or None."""
        return self.relations[0] if self.relations else None

    @property
    def url(self) -> Optional[str]:
        """The full ingress URL to reach the current unit.

        May return None if the URL isn't available yet.
        """
        relation = self.relation
        if not relation:
            return None

        # fetch the provider's app databag
        remote_data = relation.data[relation.app]
        provider_app_data = remote_data.get("data")
        if not provider_app_data:
            return None

        ingress_data = ProviderData(ingress=provider_app_data)
        return ingress_data.ingress.url
