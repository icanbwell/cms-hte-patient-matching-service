from pathlib import Path
from typing import Any, Dict, Optional


class ConfigLoader:
    """
    load different configurations for PSS
    """

    def __init__(self, config_directory: Optional[Path] = None) -> None:
        if config_directory:
            self.config_dir = config_directory
        else:
            self.config_dir = Path(__file__).parent.joinpath("../configs")
        self.__client_id_security_tag_map: Optional[Dict[str, Any]] = None
        self.__scoring_configs: Optional[Dict[str, Dict[str, Any]]] = None
