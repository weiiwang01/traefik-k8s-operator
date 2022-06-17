# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

r"""# Interface Library for ingress_per_unit.

This library wraps relation endpoints using the `ingress_per_unit` interface
and provides a Python API for both requesting and providing per-unit
ingress.

## Getting Started

To get started using the library, you just need to fetch the library using `charmcraft`.

```shell
charmcraft fetch-lib charms.traefik_k8s.v0.ingress_per_unit
```

Add the `jsonschema` dependency to the `requirements.txt` of your charm.

```yaml
requires:
    ingress:
        interface: ingress_per_unit
        limit: 1
```

Then, to initialise the library:

```python
# ...
from charms.traefik_k8s.v0.ingress_per_unit import IngressPerUnitRequirer

class SomeCharm(CharmBase):
  def __init__(self, *args):
    # ...
    self.ingress_per_unit = IngressPerUnitRequirer(self, port=80)
    # The following event is triggered when the ingress URL to be used
    # by this unit of `SomeCharm` changes or there is no longer an ingress
    # URL available, that is, `self.ingress_per_unit` would return `None`.
    self.framework.observe(
        self.ingress_per_unit.on.ingress_changed, self._handle_ingress_per_unit
    )
    # ...

    def _handle_ingress_per_unit(self, event):
        logger.info("This unit's ingress URL: %s", self.ingress_per_unit.url)
```
"""
import logging
import socket
import typing
from typing import Dict, Optional, TypeVar, Union, Tuple

import ops.model
import yaml
from ops.charm import CharmBase, RelationBrokenEvent, RelationEvent
from ops.framework import EventSource, Object, ObjectEvents, StoredState
from ops.model import (
    ActiveStatus,
    Application,
    BlockedStatus,
    Relation,
    StatusBase,
    Unit,
    WaitingStatus,
)

# The unique Charmhub library identifier, never change it
LIBID = "7ef06111da2945ed84f4f5d4eb5b353a"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 10

log = logging.getLogger(__name__)

try:
    import jsonschema

    DO_VALIDATION = True
except ModuleNotFoundError:
    log.warning(
        "The `ingress_per_unit` library needs the `jsonschema` package to be able "
        "to do runtime data validation; without it, it will still work but validation "
        "will be disabled. \n"
        "It is recommended to add `jsonschema` to the 'requirements.txt' of your charm, "
        "which will enable this feature."
    )
    DO_VALIDATION = False

# LIBRARY GLOBS
RELATION_INTERFACE = "ingress_per_unit"
DEFAULT_RELATION_NAME = RELATION_INTERFACE.replace("_", "-")

INGRESS_REQUIRES_UNIT_SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string"},
        "name": {"type": "string"},
        "host": {"type": "string"},
        "port": {"type": "string"},
    },
    "required": ["model", "name", "host", "port"],
}
INGRESS_PROVIDES_APP_SCHEMA = {
    "type": "object",
    "properties": {
        "ingress": {
            "type": "object",
            "patternProperties": {
                "": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                    },
                    "required": ["url"],
                }
            },
        },
        # Optional key for backwards compatibility
        # with legacy requirers based on SDI
        "_supported_versions": {"type": "string"},
    },
    "required": ["ingress"],
}

# TYPES
try:
    from typing import TypedDict, Literal
except ImportError:
    from typing_extensions import TypedDict, Literal  # py35 compat


class RequirerData(TypedDict):
    """Model of the data a unit implementing the requirer will need to provide."""

    model: str
    name: str
    host: str
    port: int


RequirerUnitData = Dict[Unit, "RequirerData"]
KeyValueMapping = Dict[str, str]
ProviderApplicationData = Dict[str, KeyValueMapping]


def _validate_data(data, schema):
    """Checks whether `data` matches `schema`.

    Will raise DataValidationError if the data is not valid, else return None.
    """
    if not DO_VALIDATION:
        return
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        raise DataValidationError(data, schema) from e


# EXCEPTIONS
class DataValidationError(RuntimeError):
    """Raised when data validation fails on IPU relation data."""


class RelationException(RuntimeError):
    """Base class for relation exceptions from this library.

    Attributes:
        relation: The Relation which caused the exception.
        entity: The Application or Unit which caused the exception.
    """

    def __init__(self, relation: Relation, entity: Union[Application, Unit]):
        super().__init__(relation)
        self.args = (
            "There is an error with the relation {}:{} with {}".format(
                relation.name, relation.id, entity.name
            ),
        )
        self.relation = relation
        self.entity = entity


class RelationDataMismatchError(RelationException):
    """Data from different units do not match where they should."""


class RelationPermissionError(RelationException):
    """Ingress is requested to do something for which it lacks permissions."""

    def __init__(self, relation: Relation, entity: Union[Application, Unit],
                 message: str):
        super(RelationPermissionError, self).__init__(relation, entity)
        self.args = (
            "Unable to write data to relation '{}:{}' with {}: {}".format(
                relation.name, relation.id, entity.name, message
            ),
        )


# EVENTS
class RelationAvailableEvent(RelationEvent):
    """Event triggered when a relation is ready to provide ingress."""


class RelationFailedEvent(RelationEvent):
    """Event triggered when something went wrong with a relation."""


class RelationReadyEvent(RelationEvent):
    """Event triggered when a remote relation has the expected data."""


class IngressPerUnitEvents(ObjectEvents):
    """Container for events for IngressPerUnit."""

    available = EventSource(RelationAvailableEvent)
    ready = EventSource(RelationReadyEvent)
    failed = EventSource(RelationFailedEvent)
    broken = EventSource(RelationBrokenEvent)


class _IngressPerUnitBase(Object):
    """Base class for IngressPerUnit interface classes."""

    _IngressPerUnitEventType = TypeVar("_IngressPerUnitEventType",
                                       bound=IngressPerUnitEvents)
    on: _IngressPerUnitEventType

    def __init__(self, charm: CharmBase,
                 relation_name: str = DEFAULT_RELATION_NAME):
        """Constructor for _IngressPerUnitBase.

        Args:
            charm: The charm that is instantiating the instance.
            relation_name: The name of the relation name to bind to
                (defaults to "ingress-per-unit").
        """
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
        """The list of Relation instances associated with this relation_name."""
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

    def is_ready(self, relation: Relation = None) -> bool:
        """Checks whether the given relation is ready.

        A relation is ready if the remote side has sent valid data.
        """
        if relation is None:
            return any(map(self.is_ready, self.relations))
        if relation.app is None:
            # No idea why, but this happened once.
            return False
        if not relation.app.name:  # type: ignore
            # Juju doesn't provide JUJU_REMOTE_APP during relation-broken
            # hooks. See https://github.com/canonical/operator/issues/693
            return False
        return True


class IngressPerUnitProvider(_IngressPerUnitBase):
    """Implementation of the provider of ingress_per_unit."""

    on = IngressPerUnitEvents()

    def is_ready(self, relation: Relation = None) -> bool:
        """Checks whether the given relation is ready.

        Or any relation if not specified.
        A given relation is ready if SOME remote side has sent valid data.
        """
        if relation is None:
            return any(map(self.is_ready, self.relations))

        if not super().is_ready(relation):
            return False

        try:
            requirer_units_data = self._requirer_units_data(relation)
        except Exception:
            log.exception(
                "Cannot fetch ingress data for the '{}' relation".format(
                    relation))
            return False

        return any(requirer_units_data.values())

    def validate(self, relation: Relation = None) -> bool:
        """Checks whether the given relation is failed.

        Or any relation if not specified.
        """
         # verify that all remote units (requirer's side) publish the same model.
        # We do not validate the port because, in case of changes to the configuration
        # of the charm or a new version of the charmed workload, e.g. over an upgrade,
        # the remote port may be different among units.
        expected_model = None  # It may be none for units that have not yet written data

        remote_units_data = self._requirer_units_data(relation)
        for remote_unit, remote_unit_data in remote_units_data.items():
            if "model" in remote_unit_data:
                remote_model = remote_unit_data["model"]
                if not expected_model:
                    expected_model = remote_model
                elif expected_model != remote_model:
                    raise RelationDataMismatchError(relation, remote_unit)
        return False

    def is_unit_ready(self, relation: Relation, unit: Unit) -> bool:
        """Report whether the given unit has shared data in its unit data bag."""
        # sanity check: this should not occur in production, but it may happen
        # during testing: cfr https://github.com/canonical/traefik-k8s-operator/issues/39
        assert (
                unit in relation.units
        ), "attempting to get ready state for unit that does not belong to relation"
        try:
            self._get_requirer_unit_data(relation, unit)
        except (KeyError, DataValidationError):
            return False
        return True

    def get_data(self, relation: Relation, unit: Unit) -> "RequirerData":
        """Fetch the data shared by the specified unit on the relation (Requirer side)."""
        return self._get_requirer_unit_data(relation, unit)

    def publish_url(self, relation: Relation, unit_name: str, url: str):
        """Place the ingress url in the application data bag for the units on the requires side.

        Assumes that this unit is leader.
        """
        assert self.unit.is_leader(), 'only leaders can do this'

        raw_data = relation.data[self.app].get("ingress", None)
        data = yaml.safe_load(raw_data) if raw_data else {}
        ingress = {"ingress": data}

        # we ensure that the application databag has the shape we think it
        # should have; to catch any inconsistencies early on.
        try:
            _validate_data(ingress, INGRESS_PROVIDES_APP_SCHEMA)
        except DataValidationError as e:
            log.error(
                "unable to publish url to {}: corrupted application databag ({})".format(
                    unit_name, e
                )
            )
            return

        # we update the data with a new url
        data[unit_name] = {"url": url}

        # we validate the data **again**, to ensure that we respected the schema
        # and did not accidentally corrupt our own databag.
        _validate_data(ingress, INGRESS_PROVIDES_APP_SCHEMA)
        relation.data[self.app]["ingress"] = yaml.safe_dump(data)

    def wipe_ingress_data(self, relation):
        """Remove all published ingress data.

        Assumes that this unit is leader.
        """
        assert self.unit.is_leader(), 'only leaders can do this'
        del relation.data[self.app]["ingress"]

    def _requirer_units_data(self, relation: Relation) -> RequirerUnitData:
        """Fetch and validate the requirer's units databag."""
        if not relation.app or not relation.app.name:
            # Handle edge case where remote app name can be missing, e.g.,
            # relation_broken events.
            # FIXME https://github.com/canonical/traefik-k8s-operator/issues/34
            return {}

        remote_units = [unit for unit in relation.units if
                        unit.app is not self.app]

        requirer_units_data = {}
        for remote_unit in remote_units:
            try:
                remote_data = self._get_requirer_unit_data(relation, remote_unit)
            except KeyError:
                # this remote unit didn't share data yet
                log.warning('Remote unit {} not ready.'.format(remote_unit.name))
                continue
            except DataValidationError:
                # this remote unit sent invalid data.
                log.error('Remote unit {} sent invalid data.'.format(remote_unit.name))
                continue

            remote_data['port'] = int(remote_data['port'])
            requirer_units_data[remote_unit] = remote_data
        return requirer_units_data

    def _get_requirer_unit_data(self, relation: Relation, remote_unit: Unit) -> RequirerData:
        """Attempts to fetch the requirer unit data for this unit.

        May raise KeyError if the remote unit didn't send (some of) the required
        data yet, or ValidationError if it did share some data, but the data
        is invalid.
        """
        databag = relation.data[remote_unit]
        remote_data = {k: databag[k] for k in ('port', 'host', 'model', 'name')}
        _validate_data(remote_data, INGRESS_REQUIRES_UNIT_SCHEMA)

        # do some convenience casting
        remote_data['port'] = int(remote_data['port'])
        return remote_data

    def _provider_app_data(self, relation: Relation) -> ProviderApplicationData:
        """Fetch and validate the provider's app databag."""
        if not relation.app or not relation.app.name:
            # Handle edge case where remote app name can be missing, e.g.,
            # relation_broken events.
            # FIXME https://github.com/canonical/traefik-k8s-operator/issues/34
            return {}

        provider_app_data = {}
        # we start by looking at the provider's app databag
        if self.unit.is_leader():
            # only leaders can read their app's data
            data = relation.data[self.app].get("data")
            deserialized = {}
            if data:
                deserialized = yaml.safe_load(data)
                _validate_data(deserialized, INGRESS_PROVIDES_APP_SCHEMA)
            provider_app_data = deserialized.get("ingress", {})

        return provider_app_data

    @property
    def proxied_endpoints(self) -> dict:
        """The ingress settings provided to units by this provider.

        For example, when this IngressPerUnitProvider has provided the
        `http://foo.bar/my-model.my-app-1` and
        `http://foo.bar/my-model.my-app-2` URLs to the two units of the
        my-app application, the returned dictionary will be:

        ```
        {
            "my-app/1": {
                "url": "http://foo.bar/my-model.my-app-1"
            },
            "my-app/2": {
                "url": "http://foo.bar/my-model.my-app-2"
            }
        }
        ```
        """
        results = {}

        for ingress_relation in self.relations:
            provider_app_data = self._provider_app_data(ingress_relation)
            results.update(provider_app_data)

        return results


class _IPUEvent(RelationEvent):
    __args__ = ()  # type: Tuple[str]
    __optional_kwargs__ = {}  # type: Dict[str, Any]

    @classmethod
    def __attrs__(cls):
        return cls.__args__ + tuple(cls.__optional_kwargs__.keys())

    __converters__ = (
        (Unit, "<__unit__>", "get_unit"),
        (Application, "<__app__>", "get_app"),
    )

    def __init__(self, handle, relation, *args, **kwargs):
        super().__init__(handle, relation)
        if not len(self.__args__) == len(args):
            raise TypeError("expected {} args, got {}".format(len(self.__args__), len(args)))

        for attr, obj in zip(self.__args__, args):
            setattr(self, attr, obj)
        for attr, default in self.__optional_kwargs__.items():
            obj = kwargs.get(attr, None)
            setattr(self, attr, obj)

    def _deserialize(self, obj, attr):
        for typ_, marker, meth_name in self.__converters__:
            if attr.startswith(marker):
                attr = attr.strip(marker)
                method = getattr(self.framework.model, meth_name)
                return method(obj), attr
        raise TypeError("cannot deserialize {}: no converter".format(type(obj).__name__))

    def _serialize(self, obj, attr):
        for typ_, marker, _ in self.__converters__:
            if isinstance(obj, typ_):
                return obj, marker + attr
        raise TypeError("cannot serialize {}: no converter".format(type(obj).__name__))

    def snapshot(self) -> dict:
        dct = super().snapshot()
        for attr in self.__attrs__():
            obj = getattr(self, attr)
            if isinstance(obj, (Unit, Application)):
                obj, attr = self._serialize(obj, attr)
            dct[attr] = obj
        return dct

    def restore(self, snapshot: dict) -> None:
        super().restore(snapshot)
        for attr in self.__attrs__():
            obj = snapshot[attr]
            try:
                obj, attr = self._deserialize(obj, attr)
            except TypeError as e:  # mostly safe
                pass
            setattr(self, attr, obj)


class IngressPerUnitReadyEvent(_IPUEvent):
    """Ingress is ready (or has changed) for some unit."""
    __args__ = ('unit', 'url')
    if typing.TYPE_CHECKING:
        unit = None  # type: Unit
        url = None  # type: str


class IngressPerUnitReadyForUnitEvent(_IPUEvent):
    """Ingress is ready (or has changed) for this unit.

    Is only fired on the unit(s) for which ingress has changed.
    """
    __args__ = ('url', )
    if typing.TYPE_CHECKING:
        url = None  # type: str


class IngressPerUnitRevokedEvent(RelationEvent):
    """Ingress is revoked (or has changed) for some unit."""


class IngressPerUnitRevokedForUnitEvent(RelationEvent):
    """Ingress is revoked (or has changed) for this unit.

    Is only fired on the unit(s) for which ingress has changed.
    """


class IngressPerUnitRequirerEvents(IngressPerUnitEvents):
    """Container for IUP events."""
    ready = EventSource(IngressPerUnitReadyEvent)
    revoked = EventSource(IngressPerUnitRevokedEvent)
    ready_for_unit = EventSource(IngressPerUnitReadyForUnitEvent)
    revoked_for_unit = EventSource(IngressPerUnitRevokedForUnitEvent)


class IngressPerUnitRequirer(_IngressPerUnitBase):
    """Implementation of the requirer of ingress_per_unit."""

    on = IngressPerUnitRequirerEvents()
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
            listen_to: Literal['only-this-unit', 'all-units', 'both'] = 'only-this-unit'
    ):
        """Constructor for IngressRequirer.

        The request args can be used to specify the ingress properties when the
        instance is created. If any are set, at least `port` is required, and
        they will be sent to the ingress provider as soon as it is available.
        All request args must be given as keyword args.

        Args:
            charm: the charm that is instantiating the library.
            relation_name: the name of the relation name to bind to
                (defaults to "ingress-per-unit"; relation must be of interface
                type "ingress_per_unit" and have "limit: 1")
            host: Hostname to be used by the ingress provider to address the
            requirer unit; if unspecified, the pod ip of the unit will be used
                instead
            listen_to: Choose which events should be fired on this unit:
                - only-this-unit: this unit will only be notified when ingress
                  is ready/revoked for this unit.
                - all-units: this unit will be notified when ingress is
                  ready/revoked for any unit of this application, including
                  itself.
                - all: this unit will receive both event types (which means it
                  will be notified *twice* of changes to this unit's ingress!)
        """
        super().__init__(charm, relation_name)
        self._stored.set_default(current_urls=None)

        # if instantiated with a port, and we are related, then
        # we immediately publish our ingress data  to speed up the process.
        self._auto_data = host, port if port else None
        self.listen_to = listen_to

        self.framework.observe(
            self.charm.on[self.relation_name].relation_changed,
            self._handle_relation
        )
        self.framework.observe(
            self.charm.on[self.relation_name].relation_broken,
            self._handle_relation
        )

    def _handle_relation(self, event):
        # we calculate the diff between the urls we were aware of
        # before and those we know now
        previous_urls = self._stored.current_urls or {}
        current_urls = self.urls or {}

        removed = previous_urls.keys() - current_urls.keys()
        changed = {a for a in current_urls if current_urls[a] != previous_urls.get(a)}

        this_unit_name = self.unit.name
        if self.listen_to in {'only-this-unit', 'both'}:
            if this_unit_name in changed:
                self.on.ready_for_unit.emit(self.relation,
                                            current_urls[this_unit_name])

            if this_unit_name in removed:
                self.on.revoked_for_unit.emit(self.relation)

        if self.listen_to in {'all-units', 'both'}:
            for unit_name in changed:
                unit = self.model.get_unit(unit_name)
                self.on.ready.emit(self.relation, unit, current_urls[unit_name])

            for unit_name in removed:
                self.on.revoked.emit(self.relation)

        self._stored.current_urls = current_urls
        self._publish_auto_data(event.relation)

    def _handle_upgrade_or_leader(self, event):
        for relation in self.relations:
            self._publish_auto_data(relation)

    def _publish_auto_data(self, relation: Relation):
        if self._auto_data:
            host, port = self._auto_data
            self.provide_ingress_requirements(host=host, port=port)

    @property
    def relation(self) -> Optional[Relation]:
        """The established Relation instance, or None if still unrelated."""
        return self.relations[0] if self.relations else None

    def is_ready(self) -> bool:
        """Checks whether the given relation is ready.

        Or any relation if not specified.
        A given relation is ready if the remote side has sent valid data.
        """
        if not self.relation:
            return False
        if super().is_ready(self.relation) is False:
            return False
        return bool(self.url)

    def provide_ingress_requirements(self, *, host: str = None, port: int):
        """Publishes the data that Traefik needs to provide ingress.

        Args:
            host: Hostname to be used by the ingress provider to address the
             requirer unit; if unspecified, FQDN will be used instead
            port: the port of the service (required)
        """
        if not host:
            host = socket.getfqdn()

        data = {
            "model": self.model.name,
            "name": self.unit.name,
            "host": host,
            "port": str(port),
        }
        _validate_data(data, INGRESS_REQUIRES_UNIT_SCHEMA)
        self.relation.data[self.unit].update(data)

    @property
    def urls(self) -> dict:
        """The full ingress URLs to reach every unit.

        May return an empty dict if the URLs aren't available yet.
        """
        relation = self.relation
        if not relation:
            return {}

        if not relation.app.name:  # type: ignore
            # FIXME Workaround for https://github.com/canonical/operator/issues/693
            # We must be in a relation_broken hook
            return {}

        raw = relation.data.get(relation.app, {}).get("ingress")
        if not raw:
            # remote side didn't send yet
            return {}

        data = yaml.safe_load(raw)
        _validate_data({"ingress": data}, INGRESS_PROVIDES_APP_SCHEMA)

        return {unit_name: unit_data["url"] for unit_name, unit_data in
                data.items()}

    @property
    def url(self) -> Optional[str]:
        """The full ingress URL to reach the current unit.

        May return None if the URL isn't available yet.
        """
        if not self.urls:
            return None
        return self.urls.get(self.charm.unit.name)
