from pathlib import Path
from .config import PhotographyError


def export_path(output, config, store, suffixes):
    output = Path(output).expanduser().resolve()
    if output.suffix.lower() not in suffixes or output.is_relative_to(config.state_dir):
        raise PhotographyError("INVALID_ARGUMENT", "Export to an appropriate file outside the runtime state directory.")
    for library in store.libraries():
        if output.is_relative_to(Path(library["root_path"]).resolve()):
            raise PhotographyError("INVALID_ARGUMENT", "Export must be outside original photo directories.")
    return output
