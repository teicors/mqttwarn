# -*- coding: utf-8 -*-
import dataclasses
import re
from datetime import datetime, timezone
import typing as t

from mqttwarn.context import RuntimeContext
from mqttwarn.model import Service

try:
    import json
except ImportError:
    import simplejson as json


@dataclasses.dataclass
class FrigateEvent:
    """
    Manage inbound event data received from Frigate.
    """
    time: datetime
    camera: str
    label: str
    current_zones: t.List[str]
    entered_zones: t.List[str]

    def f(self, value):
        return [y.replace('_', ' ') for y in value]

    @property
    def current_zones_str(self):
        if self.current_zones:
            return ', '.join(self.f(self.current_zones))
        else:
            return ''

    @property
    def entered_zones_str(self):
        if self.entered_zones:
            return ', '.join(self.f(self.entered_zones))
        else:
            return ''

    def to_dict(self) -> t.Dict[str, str]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class NtfyParameters:
    """
    Manage outbound parameter data for Apprise/Ntfy.
    """
    title: str
    format: str
    click: str
    attach: t.Optional[str] = None

    def to_dict(self) -> t.Dict[str, str]:
        data = dataclasses.asdict(self)
        data = {k: v for (k, v) in data.items() if v is not None}
        return data


def frigate_events(topic, data, srv: Service):
    """
    mqttwarn transformation function which computes options to be submitted to Apprise/Ntfy.
    """

    # Acceptable hack to get attachment filename template from service configuration.
    context: RuntimeContext = srv.mwcore["context"]
    service_config = context.get_service_config("apprise-ntfy")
    filename_template = service_config.get("filename_template")

    # Decode JSON message.
    after = json.loads(data['payload'])['after']

    # Collect details from inbound Frigate event.
    event = FrigateEvent(
        time=datetime.fromtimestamp(after['frame_time'], tz=timezone.utc),
        camera=after['camera'],
        label=after['sub_label'] or after['label'],
        current_zones=after['current_zones'],
        entered_zones=after['entered_zones'],
    )

    # Interpolate event data into attachment filename template.
    attach_filename = filename_template.format(**event.to_dict())

    # Compute parameters for outbound Apprise / Ntfy URL.
    ntfy_parameters = NtfyParameters(
        title=f"{event.label} entered {event.entered_zones_str} at {event.time}",
        format=f"{event.label} was in {event.current_zones_str}",
        click=f"https://frigate/events?camera={event.camera}&label={event.label}&zone={event.entered_zones[0]}",
        #attach=attach_filename,
    )
    return ntfy_parameters.to_dict()


def frigate_events_filter(topic, message, section, srv: Service):
    """
    mqttwarn filter function to only use `new` and important `update` Frigate events.

    Additionally, validate more details within the event message,
    specifically the `after` section. For example, skip false positives.

    :return: True if message should be filtered, i.e. notification should be skipped.
    """
    try:
        message = json.loads(message)
    except json.JSONDecodeError as e:
        srv.logging.warning(f"Can't parse Frigate event message: {e}")
        return True

    # ignore ending messages
    message_type = message.get('type', None)
    if message_type == 'end':
        srv.logging.warning(f"Frigate event skipped, ignoring Message type '{message_type}'")
        return True

    # payload must have 'after' key
    elif "after" not in message:
        srv.logging.warning("Frigate event skipped, 'after' missing from payload")
        return True

    after = message.get('after')

    nonempty_fields = ['false_positive', 'camera', 'label', 'current_zones', 'entered_zones', 'frame_time']
    for field in nonempty_fields:

        # Validate field exists.
        if field not in after:
            srv.logging.warning(f"Frigate event skipped, missing field: {field}")
            return True

        value = after.get(field)

        # We can ignore if `current_zones` is empty.
        if field == "current_zones":
            continue

        # Check if it's a false positive.
        if field == "false_positive":
            if value is True:
                srv.logging.warning("Frigate event skipped, it is a false positive")
                return True
            else:
                continue

        # All other keys should be present and have values.
        if not value:
            srv.logging.warning(f"Frigate event skipped, field is empty: {field}")
            return True

    # Ignore unimportant `update` events.
    before = message.get('before')
    if message_type == 'update' and isinstance(before, dict):
        if before.get('stationary') is True and after.get('stationary') is True:
            srv.logging.warning("Frigate event skipped, object is stationary")
            return True
        elif (after['current_zones'] == after['entered_zones'] or
                (before['current_zones'] == after['current_zones'] and
                 before['entered_zones'] == after['entered_zones'])):
            srv.logging.warning("Frigate event skipped, object stayed within same zone")
            return True

    # Evaluate optional skip rules.
    context: RuntimeContext = srv.mwcore["context"]
    frigate_skip_rules = context.config.getdict(section, "frigate_skip_rules")
    for rule in frigate_skip_rules.values():
        do_skip = True
        for fieldname, skip_values in rule.items():
            actual_value = after[fieldname]
            if isinstance(actual_value, list):
                do_skip = do_skip and all(value in skip_values for value in actual_value)
            else:
                do_skip = do_skip and actual_value in skip_values
        if do_skip:
            srv.logging.warning("Frigate event skipped, object did not enter zone of interest")
            return True

    return False


def frigate_snapshot_decode_topic(topic, data, srv: Service):
    """
    Decode Frigate MQTT topic for image snapshots.

    frigate/+/+/snapshot

    See also:
    - https://docs.frigate.video/integrations/mqtt/#frigatecamera_nameobject_namesnapshot
    """
    if type(topic) == str:
        try:
            pattern = r'^frigate/(?P<camera_name>.+?)/(?P<object_name>.+?)/snapshot$'
            p = re.compile(pattern)
            m = p.match(topic)
            topology = m.groupdict()
        except:
            topology = {}
        return topology
    return None
