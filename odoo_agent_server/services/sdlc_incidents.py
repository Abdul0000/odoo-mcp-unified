import json

from fastapi import HTTPException

from ..config import settings


def readiness_incidents():
    path = settings.state_path / "readiness-incidents.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return {"observed": True, "incidents": list(reversed(state["incidents"]))}
    except FileNotFoundError:
        return {"observed": False, "incidents": []}
    except (OSError, ValueError, TypeError, KeyError):
        raise HTTPException(503, "Readiness incident state is unreadable") from None
