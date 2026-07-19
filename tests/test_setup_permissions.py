from dataclasses import dataclass
import inspect
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
import unittest

from input_action_controller.devices.discovery import DeviceCandidate
from input_action_controller.setup.devices import SelectorDraft
from input_action_controller.setup.permissions import (
    DeviceInstanceObservation,
    GROUP_FALLBACK_WARNING,
    INPUT_GROUP_ACCESS,
    InvalidManagedDestination,
    PermissionTransaction,
    ReconnectVerificationError,
    RenderedRule,
    ScopeReport,
    UnreadableManagedRule,
    render_rule,
    verify_reconnected_access,
)


def candidate(
    node: str,
    *,
    subsystem: str,
    vendor_id: str = "047f",
    product_id: str = "c056",
    interface_number: str | None = "03",
    serial: str | None = None,
    id_path: str | None = None,
    classifier: tuple[str, str] | None = None,
    keyboard_class: bool = False,
) -> DeviceCandidate:
    properties = {
        "ID_VENDOR_ID": vendor_id,
        "ID_MODEL_ID": product_id,
        "DEVNAME": node,
    }
    if interface_number is not None:
        properties["ID_USB_INTERFACE_NUM"] = interface_number
    if serial is not None:
        properties["ID_SERIAL_SHORT"] = serial
    if id_path is not None:
        properties["ID_PATH"] = id_path
    if classifier is not None:
        properties[classifier[0]] = classifier[1]
    return DeviceCandidate(
        node=node,
        subsystem=subsystem,
        properties=properties,
        event_codes=frozenset(),
        keyboard_class=keyboard_class,
        classifiers=(() if classifier is None else (classifier,)),
    )


@dataclass(frozen=True)
class RunCall:
    argv: tuple[str, ...]
    check: bool
    shell: bool


class FakeRunner:
    def __init__(self, *, fail_calls: set[int] | None = None):
        self.calls: list[RunCall] = []
        self.fail_calls = set() if fail_calls is None else fail_calls

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        check: bool,
        shell: bool,
    ) -> subprocess.CompletedProcess:
        self.calls.append(RunCall(argv, check, shell))
        if len(self.calls) in self.fail_calls:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)


def hidraw_selectors(**overrides) -> SelectorDraft:
    values = {
        "transport": "hidraw",
        "vendor_id": "047f",
        "product_id": "c056",
        "interface_number": "03",
        "serial": "SERIAL-1",
        "id_path": None,
        "classifier": None,
    }
    values.update(overrides)
    return SelectorDraft(**values)


def evdev_selectors(**overrides) -> SelectorDraft:
    values = {
        "transport": "evdev",
        "vendor_id": "1234",
        "product_id": "5678",
        "interface_number": "01",
        "serial": None,
        "id_path": "pci-0000:00:14.0-usb-0:2:1.1",
        "classifier": ("ID_INPUT_MOUSE", "1"),
    }
    values.update(overrides)
    return SelectorDraft(**values)


class RuleRenderingTests(unittest.TestCase):
    def test_renders_a_deterministic_hidraw_rule_and_managed_filename(self):
        selectors = hidraw_selectors()

        rendered = render_rule("Desk Headset / Main", selectors, ())
        repeated = render_rule("Desk Headset / Main", selectors, ())

        self.assertEqual(rendered, repeated)
        self.assertEqual(
            rendered.destination,
            Path(
                "/etc/udev/rules.d/"
                "70-input-action-controller-"
                "desk-headset-main-"
                "9dba1aab5563fe071b616ad92b0e45bf27528bd3741e029cda43f81081f0ce7c-"
                "e5682bb91e92.rules"
            ),
        )
        self.assertEqual(
            rendered.content,
            'ACTION=="add", SUBSYSTEM=="hidraw", KERNEL=="hidraw*", '
            'ATTRS{idVendor}=="047f", ATTRS{idProduct}=="c056", '
            'ENV{ID_USB_INTERFACE_NUM}=="03", '
            'ENV{ID_SERIAL_SHORT}=="SERIAL-1", TAG+="uaccess"\n',
        )

    def test_renders_evdev_classifier_and_optional_id_path(self):
        rendered = render_rule("Desk button", evdev_selectors(), ())

        self.assertEqual(
            rendered.content,
            'ACTION=="add", SUBSYSTEM=="input", KERNEL=="event*", '
            'ATTRS{idVendor}=="1234", ATTRS{idProduct}=="5678", '
            'ENV{ID_USB_INTERFACE_NUM}=="01", '
            'ENV{ID_PATH}=="pci-0000:00:14.0-usb-0:2:1.1", '
            'ENV{ID_INPUT_MOUSE}=="1", TAG+="uaccess"\n',
        )
        self.assertEqual(rendered.scope.broadened_fields, ("event_codes",))

    def test_renders_registered_tablet_classifier(self):
        rendered = render_rule(
            "Tablet control",
            evdev_selectors(classifier=("ID_INPUT_TABLET", "1"), id_path=None),
            (),
        )

        self.assertIn('ENV{ID_INPUT_TABLET}=="1"', rendered.content)

    def test_selector_hash_changes_when_a_selector_changes_and_is_lowercase(self):
        first = render_rule("Desk button", evdev_selectors(), ())
        second = render_rule(
            "Desk button",
            evdev_selectors(interface_number="02"),
            (),
        )

        first_hash = first.destination.stem.rsplit("-", 1)[1]
        second_hash = second.destination.stem.rsplit("-", 1)[1]
        self.assertRegex(first_hash, r"^[0-9a-f]{12}$")
        self.assertNotEqual(first_hash, second_hash)

    def test_distinct_profile_names_cannot_collide_after_slug_loss(self):
        long_prefix = "a" * 80
        profile_pairs = (
            ("Desk Button", "desk-button"),
            (f"{long_prefix} first", f"{long_prefix} second"),
        )

        for first_name, second_name in profile_pairs:
            with self.subTest(first=first_name, second=second_name):
                first = render_rule(first_name, evdev_selectors(), ())
                second = render_rule(second_name, evdev_selectors(), ())

                self.assertNotEqual(first.destination, second.destination)
                self.assertEqual(
                    first.destination.stem.rsplit("-", 1)[1],
                    second.destination.stem.rsplit("-", 1)[1],
                )

    def test_rejects_unsupported_transport_classifier_and_control_characters(self):
        with self.assertRaisesRegex(ValueError, "transport"):
            render_rule(
                "Unsupported",
                hidraw_selectors(transport="usb"),
                (),
            )
        with self.assertRaisesRegex(ValueError, "classifier"):
            render_rule(
                "Hidraw classifier",
                hidraw_selectors(classifier=("ID_INPUT_MOUSE", "1")),
                (),
            )
        with self.assertRaisesRegex(ValueError, "classifier"):
            render_rule(
                "Unknown classifier",
                evdev_selectors(classifier=("ID_INPUT_VENDOR_SPECIAL", "1")),
                (),
            )
        with self.assertRaisesRegex(ValueError, "classifier"):
            render_rule(
                "Invalid classifier value",
                evdev_selectors(classifier=("ID_INPUT_TABLET", "0")),
                (),
            )
        with self.assertRaisesRegex(ValueError, "control"):
            render_rule(
                "Injected serial",
                hidraw_selectors(serial='unsafe"\nvalue'),
                (),
            )

    def test_group_fallback_requires_an_explicit_access_choice_and_reports_its_risk(
        self,
    ):
        default = render_rule("Desk button", evdev_selectors(), ())
        fallback = render_rule(
            "Desk button",
            evdev_selectors(),
            (),
            access=INPUT_GROUP_ACCESS,
        )

        self.assertIn('TAG+="uaccess"', default.content)
        self.assertNotIn('GROUP="input"', default.content)
        self.assertIn('GROUP="input", MODE="0660"', fallback.content)
        self.assertNotIn('TAG+="uaccess"', fallback.content)
        self.assertEqual(fallback.access_warning, GROUP_FALLBACK_WARNING)
        self.assertIn("other matching input nodes", fallback.scope.future_predicate)
        with self.assertRaisesRegex(ValueError, "access"):
            render_rule("Desk button", evdev_selectors(), (), access="group")


class ScopeReportingTests(unittest.TestCase):
    def test_lists_every_current_node_matching_the_rendered_predicate(self):
        selectors = evdev_selectors()
        matching_keyboard = candidate(
            "/dev/input/event7",
            subsystem="input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            id_path=selectors.id_path,
            classifier=selectors.classifier,
            keyboard_class=True,
        )
        matching_pointer = candidate(
            "/dev/input/event4",
            subsystem="input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            id_path=selectors.id_path,
            classifier=selectors.classifier,
        )
        wrong_classifier = candidate(
            "/dev/input/event5",
            subsystem="input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            id_path=selectors.id_path,
            classifier=("ID_INPUT_KEYBOARD", "1"),
        )
        wrong_kernel = candidate(
            "/dev/hidraw3",
            subsystem="input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            id_path=selectors.id_path,
            classifier=selectors.classifier,
        )

        scope = render_rule(
            "Desk button",
            selectors,
            (
                matching_keyboard,
                wrong_classifier,
                wrong_kernel,
                matching_pointer,
            ),
        ).scope

        self.assertEqual(
            scope.current_nodes,
            ("/dev/input/event4", "/dev/input/event7"),
        )
        self.assertTrue(scope.keyboard_class)

    def test_reports_future_scope_when_no_current_node_matches(self):
        scope = render_rule("Headset", hidraw_selectors(), ()).scope

        self.assertEqual(scope.current_nodes, ())
        self.assertEqual(scope.broadened_fields, ())
        self.assertIn("later device", scope.future_predicate)
        self.assertIn('SUBSYSTEM=="hidraw"', scope.future_predicate)
        self.assertIn('ENV{ID_SERIAL_SHORT}=="SERIAL-1"', scope.future_predicate)
        self.assertFalse(scope.keyboard_class)

    def test_keyboard_classifier_marks_future_scope_without_a_current_node(self):
        scope = render_rule(
            "Keyboard button",
            evdev_selectors(
                classifier=("ID_INPUT_KEYBOARD", "1"),
                id_path=None,
            ),
            (),
        ).scope

        self.assertTrue(scope.keyboard_class)

    def test_id_input_key_marks_future_scope_as_key_class(self):
        scope = render_rule(
            "Media key",
            evdev_selectors(classifier=("ID_INPUT_KEY", "1"), id_path=None),
            (),
        ).scope

        self.assertTrue(scope.keyboard_class)


class PermissionTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = self.enterContext(TemporaryDirectory())
        self.staging_directory = Path(self.temporary_directory)
        self.rendered = render_rule("Headset", hidraw_selectors(), ())

    def transaction(
        self,
        runner: FakeRunner,
        *,
        existing_rule_reader=lambda _path: None,
        rendered: RenderedRule | None = None,
    ) -> PermissionTransaction:
        return PermissionTransaction(
            self.rendered if rendered is None else rendered,
            runner,
            staging_directory=self.staging_directory,
            existing_rule_reader=existing_rule_reader,
        )

    def test_install_uses_fixed_absolute_argv_without_a_shell_and_stages_mode_0600(
        self,
    ):
        runner = FakeRunner()
        transaction = self.transaction(runner)

        transaction.install()

        staging = transaction.staging_path
        self.assertIsNotNone(staging)
        assert staging is not None
        self.assertEqual(stat.S_IMODE(staging.stat().st_mode), 0o600)
        self.assertEqual(
            runner.calls,
            [
                RunCall(
                    (
                        "/usr/bin/sudo",
                        "--",
                        "/usr/bin/install",
                        "-m",
                        "0644",
                        str(staging),
                        str(self.rendered.destination),
                    ),
                    True,
                    False,
                ),
                RunCall(
                    (
                        "/usr/bin/sudo",
                        "--",
                        "/usr/bin/udevadm",
                        "control",
                        "--reload-rules",
                    ),
                    True,
                    False,
                ),
            ],
        )

        transaction.finalize()

        self.assertFalse(staging.exists())

    def test_prepare_is_non_privileged_idempotent_and_exposes_preview_commands(self):
        runner = FakeRunner()
        read_calls: list[Path] = []
        transaction = self.transaction(
            runner,
            existing_rule_reader=lambda path: read_calls.append(path) or None,
        )

        transaction.prepare()
        staging = transaction.staging_path
        transaction.prepare()

        assert staging is not None
        self.assertEqual(transaction.staging_path, staging)
        self.assertEqual(read_calls, [self.rendered.destination])
        self.assertEqual(runner.calls, [])
        self.assertTrue(staging.exists())
        self.assertEqual(stat.S_IMODE(staging.stat().st_mode), 0o600)
        expected = (
            (
                "/usr/bin/sudo",
                "--",
                "/usr/bin/install",
                "-m",
                "0644",
                str(staging),
                str(self.rendered.destination),
            ),
            (
                "/usr/bin/sudo",
                "--",
                "/usr/bin/udevadm",
                "control",
                "--reload-rules",
            ),
        )
        self.assertEqual(transaction.preview_commands, expected)
        self.assertEqual(transaction.recovery_commands, expected)

    def test_rollback_removes_a_new_rule_then_reloads_and_cleans_staging(self):
        runner = FakeRunner()
        transaction = self.transaction(runner)
        transaction.install()
        staging = transaction.staging_path
        assert staging is not None

        transaction.rollback()

        self.assertEqual(
            runner.calls[2:],
            [
                RunCall(
                    (
                        "/usr/bin/sudo",
                        "--",
                        "/usr/bin/rm",
                        "-f",
                        "--",
                        str(self.rendered.destination),
                    ),
                    True,
                    False,
                ),
                RunCall(
                    (
                        "/usr/bin/sudo",
                        "--",
                        "/usr/bin/udevadm",
                        "control",
                        "--reload-rules",
                    ),
                    True,
                    False,
                ),
            ],
        )
        self.assertFalse(staging.exists())

    def test_rollback_reinstalls_exact_prior_bytes_and_mode(self):
        runner = FakeRunner()
        old_content = b"# exact prior bytes\n\xff"
        transaction = self.transaction(
            runner,
            existing_rule_reader=lambda _path: (old_content, 0o640),
        )
        transaction.install()
        rollback_staging = transaction.rollback_staging_path
        assert rollback_staging is not None

        self.assertEqual(rollback_staging.read_bytes(), old_content)
        self.assertEqual(stat.S_IMODE(rollback_staging.stat().st_mode), 0o600)

        transaction.rollback()

        self.assertEqual(
            runner.calls[2],
            RunCall(
                (
                    "/usr/bin/sudo",
                    "--",
                    "/usr/bin/install",
                    "-m",
                    "0640",
                    str(rollback_staging),
                    str(self.rendered.destination),
                ),
                True,
                False,
            ),
        )
        self.assertFalse(rollback_staging.exists())

    def test_nonzero_install_is_uncertain_and_recovers_a_formerly_absent_rule(self):
        runner = FakeRunner(fail_calls={1})
        transaction = self.transaction(runner)

        with self.assertRaises(subprocess.CalledProcessError):
            transaction.install()

        staging = transaction.staging_path
        assert staging is not None
        self.assertTrue(staging.exists())
        self.assertEqual(stat.S_IMODE(staging.stat().st_mode), 0o600)
        self.assertEqual(
            transaction.recovery_commands,
            (
                (
                    "/usr/bin/sudo",
                    "--",
                    "/usr/bin/rm",
                    "-f",
                    "--",
                    str(self.rendered.destination),
                ),
                (
                    "/usr/bin/sudo",
                    "--",
                    "/usr/bin/udevadm",
                    "control",
                    "--reload-rules",
                ),
            ),
        )
        self.assertEqual(transaction.remaining_scope, self.rendered.scope)
        self.assertFalse(transaction.destination_write_succeeded)

    def test_nonzero_install_recovers_exact_recorded_prior_state(self):
        old_content = b"# prior managed rule\n"
        runner = FakeRunner(fail_calls={1})
        transaction = self.transaction(
            runner,
            existing_rule_reader=lambda _path: (old_content, 0o620),
        )

        with self.assertRaises(subprocess.CalledProcessError):
            transaction.install()

        rollback_staging = transaction.rollback_staging_path
        assert rollback_staging is not None
        self.assertEqual(rollback_staging.read_bytes(), old_content)
        self.assertEqual(
            transaction.recovery_commands[0],
            (
                "/usr/bin/sudo",
                "--",
                "/usr/bin/install",
                "-m",
                "0620",
                str(rollback_staging),
                str(self.rendered.destination),
            ),
        )

    def test_failed_rollback_retains_staging_and_exact_recovery_commands(self):
        runner = FakeRunner(fail_calls={3})
        transaction = self.transaction(runner)
        transaction.install()
        staging = transaction.staging_path
        assert staging is not None

        with self.assertRaises(subprocess.CalledProcessError):
            transaction.rollback()

        self.assertTrue(staging.exists())
        self.assertEqual(stat.S_IMODE(staging.stat().st_mode), 0o600)
        self.assertEqual(
            transaction.recovery_commands,
            (
                (
                    "/usr/bin/sudo",
                    "--",
                    "/usr/bin/rm",
                    "-f",
                    "--",
                    str(self.rendered.destination),
                ),
                (
                    "/usr/bin/sudo",
                    "--",
                    "/usr/bin/udevadm",
                    "control",
                    "--reload-rules",
                ),
            ),
        )
        self.assertEqual(transaction.remaining_scope, self.rendered.scope)

    def test_reload_failure_is_recoverable_as_a_changed_destination(self):
        runner = FakeRunner(fail_calls={2})
        transaction = self.transaction(runner)

        with self.assertRaises(subprocess.CalledProcessError):
            transaction.install()

        self.assertTrue(transaction.destination_write_succeeded)
        self.assertEqual(
            transaction.recovery_commands[0],
            (
                "/usr/bin/sudo",
                "--",
                "/usr/bin/rm",
                "-f",
                "--",
                str(self.rendered.destination),
            ),
        )

        transaction.rollback()

        self.assertEqual(len(runner.calls), 4)

    def test_refuses_an_unreadable_preexisting_managed_rule_before_elevation(self):
        runner = FakeRunner()

        def unreadable(_path: Path):
            raise PermissionError("injected unreadable rule")

        transaction = self.transaction(runner, existing_rule_reader=unreadable)

        with self.assertRaises(UnreadableManagedRule):
            transaction.install()

        self.assertEqual(runner.calls, [])
        self.assertIsNone(transaction.staging_path)

    def test_refuses_destinations_outside_the_managed_directory_or_filename_scheme(
        self,
    ):
        scope = ScopeReport((), "future", (), False)
        invalid_rules = (
            RenderedRule(
                Path("/tmp/70-input-action-controller-test-123456789abc.rules"),
                "",
                scope,
            ),
            RenderedRule(Path("/etc/udev/rules.d/99-unmanaged.rules"), "", scope),
            RenderedRule(
                Path(
                    "/etc/udev/rules.d/"
                    "70-input-action-controller-test-123456789ABC.rules"
                ),
                "",
                scope,
            ),
        )

        for rendered in invalid_rules:
            with self.subTest(destination=rendered.destination):
                with self.assertRaises(InvalidManagedDestination):
                    self.transaction(FakeRunner(), rendered=rendered)


class ReconnectVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selectors = evdev_selectors(id_path=None)

    def matching_candidate(self, node: str) -> DeviceCandidate:
        return candidate(
            node,
            subsystem="input",
            vendor_id="1234",
            product_id="5678",
            interface_number="01",
            classifier=("ID_INPUT_MOUSE", "1"),
        )

    def observation(
        self,
        node: str,
        instance_id: str,
    ) -> DeviceInstanceObservation:
        return DeviceInstanceObservation(
            candidate=self.matching_candidate(node),
            instance_id=instance_id,
        )

    def test_accepts_the_same_node_name_for_a_new_event_instance_and_runs_all_checks(
        self,
    ):
        observe_calls = 0
        access_calls: list[str] = []
        acl_calls: list[str] = []
        resolution_calls: list[str] = []

        def observe_after_udev_event():
            nonlocal observe_calls
            observe_calls += 1
            return self.observation("/dev/input/event4", "sysfs-instance-new")

        result = verify_reconnected_access(
            self.selectors,
            previous_instance_ids=("sysfs-instance-old",),
            observe_after_udev_event=observe_after_udev_event,
            access_checker=lambda item: access_calls.append(item.node) or True,
            acl_checker=lambda item: acl_calls.append(item.node) or True,
            production_resolution_checker=lambda item: (
                resolution_calls.append(item.node) or True
            ),
        )

        self.assertEqual(observe_calls, 1)
        self.assertEqual(result.node, "/dev/input/event4")
        self.assertEqual(access_calls, ["/dev/input/event4"])
        self.assertEqual(acl_calls, ["/dev/input/event4"])
        self.assertEqual(resolution_calls, ["/dev/input/event4"])

    def test_rejects_an_old_instance_even_when_its_node_name_changes(self):
        with self.assertRaisesRegex(ReconnectVerificationError, "old device instance"):
            verify_reconnected_access(
                self.selectors,
                previous_instance_ids=("sysfs-instance-old",),
                observe_after_udev_event=lambda: self.observation(
                    "/dev/input/event9",
                    "sysfs-instance-old",
                ),
                access_checker=lambda _item: True,
                acl_checker=lambda _item: True,
                production_resolution_checker=lambda _item: True,
            )

    def test_requires_access_acl_and_production_resolution(self):
        with self.assertRaisesRegex(ReconnectVerificationError, "read access"):
            verify_reconnected_access(
                self.selectors,
                previous_instance_ids=(),
                observe_after_udev_event=lambda: self.observation(
                    "/dev/input/event9",
                    "sysfs-instance-new",
                ),
                access_checker=lambda _item: False,
                acl_checker=lambda _item: True,
                production_resolution_checker=lambda _item: True,
            )

        with self.assertRaisesRegex(ReconnectVerificationError, "ACL"):
            verify_reconnected_access(
                self.selectors,
                previous_instance_ids=(),
                observe_after_udev_event=lambda: self.observation(
                    "/dev/input/event9",
                    "sysfs-instance-new",
                ),
                access_checker=lambda _item: True,
                acl_checker=lambda _item: False,
                production_resolution_checker=lambda _item: True,
            )

        with self.assertRaisesRegex(ReconnectVerificationError, "production"):
            verify_reconnected_access(
                self.selectors,
                previous_instance_ids=(),
                observe_after_udev_event=lambda: self.observation(
                    "/dev/input/event9",
                    "sysfs-instance-new",
                ),
                access_checker=lambda _item: True,
                acl_checker=lambda _item: True,
                production_resolution_checker=lambda _item: False,
            )

    def test_acl_and_production_resolution_checkers_are_required_parameters(self):
        parameters = inspect.signature(verify_reconnected_access).parameters

        self.assertIs(parameters["acl_checker"].default, inspect.Parameter.empty)
        self.assertIs(
            parameters["production_resolution_checker"].default,
            inspect.Parameter.empty,
        )

    def test_rejects_empty_instance_identity_and_nonmatching_observation(self):
        with self.assertRaisesRegex(ReconnectVerificationError, "instance identity"):
            verify_reconnected_access(
                self.selectors,
                previous_instance_ids=(),
                observe_after_udev_event=lambda: self.observation(
                    "/dev/input/event9",
                    "",
                ),
                access_checker=lambda _item: True,
                acl_checker=lambda _item: True,
                production_resolution_checker=lambda _item: True,
            )

        with self.assertRaisesRegex(ReconnectVerificationError, "selectors"):
            verify_reconnected_access(
                self.selectors,
                previous_instance_ids=(),
                observe_after_udev_event=lambda: DeviceInstanceObservation(
                    candidate=candidate(
                        "/dev/input/event9",
                        subsystem="input",
                        vendor_id="9999",
                        product_id="5678",
                        interface_number="01",
                        classifier=("ID_INPUT_MOUSE", "1"),
                    ),
                    instance_id="sysfs-instance-new",
                ),
                access_checker=lambda _item: True,
                acl_checker=lambda _item: True,
                production_resolution_checker=lambda _item: True,
            )

    def test_rejects_malformed_selectors_before_observing_the_reconnect(self):
        observe_calls = 0

        def observe_after_udev_event():
            nonlocal observe_calls
            observe_calls += 1
            return self.observation("/dev/input/event9", "sysfs-instance-new")

        with self.assertRaisesRegex(ValueError, "invalid USB vendor ID"):
            verify_reconnected_access(
                evdev_selectors(vendor_id="bad"),
                previous_instance_ids=(),
                observe_after_udev_event=observe_after_udev_event,
                access_checker=lambda _item: True,
                acl_checker=lambda _item: True,
                production_resolution_checker=lambda _item: True,
            )

        self.assertEqual(observe_calls, 0)
