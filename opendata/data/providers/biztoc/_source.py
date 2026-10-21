"""Source identity and credential variable for the Biztoc RapidAPI source."""

from pathlib import Path
from typing import Final

SOURCE = Path(__file__).resolve().parent.name
CREDENTIAL_ENV_VAR: Final = "BIZTOC_" + "API_KEY"
