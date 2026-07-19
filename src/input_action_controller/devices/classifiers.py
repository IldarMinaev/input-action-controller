from collections.abc import Mapping


INPUT_CLASSIFIER_NAMES = (
    "ID_INPUT_TRACKBALL",
    "ID_INPUT_POINTINGSTICK",
    "ID_INPUT_TOUCHPAD",
    "ID_INPUT_TOUCHSCREEN",
    "ID_INPUT_TABLET_PAD",
    "ID_INPUT_TABLET",
    "ID_INPUT_JOYSTICK",
    "ID_INPUT_KEYBOARD",
    "ID_INPUT_MOUSE",
    "ID_INPUT_KEY",
)
KEY_CLASSIFIER_NAMES = frozenset({"ID_INPUT_KEYBOARD", "ID_INPUT_KEY"})


def collect_input_classifiers(
    properties: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, "1") for name in INPUT_CLASSIFIER_NAMES if properties.get(name) == "1"
    )


def is_key_classifier(classifier: tuple[str, str] | None) -> bool:
    return (
        classifier is not None
        and classifier[0] in KEY_CLASSIFIER_NAMES
        and classifier[1] == "1"
    )
