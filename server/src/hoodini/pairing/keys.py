"""Private local provisioning material; never include values in diagnostics."""

import hashlib
import os
import tempfile
from pathlib import Path

# AUTH_PWD from Elica Connect Android app IotProdServerConfiguration.getUserLevelPassword.
# One value shared by every hood; it only works while the hood is in physical setup
# mode. `hoodini extract-key` re-derives it from an APK. Empty means not bundled.
DEFAULT_SETUP_KEY = "yxB)6/Y6vG"  # noqa: S105, RUF100  # noqa: S105


class ExtractionError(ValueError):
    """Unsupported or ambiguous DEX method structure."""


def _literal_from_classes(classes: list[dict]) -> str:
    selected = [
        cls
        for cls in classes
        if cls["name"].rsplit("/", 1)[-1] == "IotProdServerConfiguration;"
    ]
    if len(selected) != 1:
        raise ExtractionError("Expected exactly one provisioning class")
    methods = [
        method
        for method in selected[0]["methods"]
        if method["name"] == "getUserLevelPassword"
        and method["descriptor"] == "()Ljava/lang/String;"
    ]
    if len(methods) != 1:
        raise ExtractionError("Expected exactly one setup password method")
    instructions = methods[0]["instructions"]
    if (
        len(instructions) != 2
        or instructions[0][0] not in ("const-string", "const-string/jumbo")
        or instructions[1] != ["return-object", instructions[0][1]]
        or not isinstance(instructions[0][2], str)
        or not instructions[0][2]
    ):
        raise ExtractionError("Setup password is not a single returned literal")
    return instructions[0][2]


def extract_setup_key(apk_path: Path) -> str:
    try:
        from androguard.misc import AnalyzeAPK
    except ImportError:
        raise ExtractionError(
            "Install hoodini[extract] to extract a setup key"
        ) from None
    try:
        _, dex_files, _ = AnalyzeAPK(str(apk_path))
        classes = []
        for dex in dex_files:
            for cls in dex.get_classes():
                if cls.get_name().rsplit("/", 1)[-1] != "IotProdServerConfiguration;":
                    continue
                methods = []
                for method in cls.get_methods():
                    if (method.get_name(), method.get_descriptor()) != (
                        "getUserLevelPassword",
                        "()Ljava/lang/String;",
                    ):
                        continue
                    instructions = []
                    for instruction in method.get_instructions():
                        name = instruction.get_name()
                        if name in ("const-string", "const-string/jumbo"):
                            # Dalvik string reference is resolved by the owning DEX pool.
                            register = f"v{instruction.get_raw()[1]}"
                            instructions.append(
                                [
                                    name,
                                    register,
                                    dex.get_cm_string(instruction.get_ref_kind()),
                                ]
                            )
                        elif name == "return-object":
                            instructions.append([name, f"v{instruction.get_raw()[1]}"])
                        else:
                            instructions.append([name])
                    methods.append(
                        {
                            "name": method.get_name(),
                            "descriptor": method.get_descriptor(),
                            "instructions": instructions,
                        }
                    )
                classes.append({"name": cls.get_name(), "methods": methods})
        return _literal_from_classes(classes)
    except ExtractionError:
        raise
    except Exception:  # noqa: BLE001 - sanitize external errors before display
        raise ExtractionError("Could not read supported APK structure") from None


def _write(path: Path, value: str) -> None:
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix=".hoodini-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_setup_key(value: str, config_dir: Path) -> None:
    _write(config_dir / "setup_key", value)


def load_setup_key(config_dir: Path) -> str:
    return (config_dir / "setup_key").read_text(encoding="utf-8")


def resolve_setup_key(config_dir: Path) -> str:
    try:
        saved = load_setup_key(config_dir)
    except FileNotFoundError:
        saved = ""
    if saved:
        return saved
    if DEFAULT_SETUP_KEY:
        return DEFAULT_SETUP_KEY
    raise ValueError("No setup key is bundled or saved; run hoodini extract-key APK")


def _token_path(server: str, config_dir: Path) -> Path:
    return config_dir / ("token-" + hashlib.sha256(server.encode()).hexdigest())


def save_token(server: str, token: str, config_dir: Path) -> None:
    _write(_token_path(server, config_dir), token)


def load_token(server: str, config_dir: Path) -> str | None:
    try:
        return _token_path(server, config_dir).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
