"""Validate the existing declarative spec without importing the reference project."""

from copy import deepcopy

from .errors import FormsError

TYPES = {
    "Text": "Question.TextField",
    "Choice": "Question.Choice",
    "Upload": "Question.FileUpload",
    "Section": "Question.ColumnGroup",
}


def validate_spec(document: list | dict) -> list[dict]:
    spec = document.get("spec") if isinstance(document, dict) else document
    if not isinstance(spec, list) or not spec:
        raise FormsError("invalid_spec", "Provide a nonempty ordered spec array.")
    keys = set()
    result = []
    for index, original in enumerate(spec):
        if not isinstance(original, dict):
            raise FormsError("invalid_spec", f"Card {index + 1} must be an object.")
        c = deepcopy(original)
        if "opts_from" in c:
            raise FormsError(
                "invalid_spec",
                "Replace opts_from with an explicit opts array; no external modules are imported.",
            )
        section = "s" in c
        allowed = {"s"} if section else {"k", "t", "q", "req", "num", "long", "opts", "mb"}
        if set(c) - allowed:
            raise FormsError(
                "invalid_spec",
                f"Unsupported fields on card {index + 1}: {sorted(set(c) - allowed)}",
            )
        title = c.get("s" if section else "q")
        if (
            not isinstance(title, str)
            or not title.strip()
            or title.strip() in {"Question", "Section"}
        ):
            raise FormsError(
                "invalid_spec", "Card titles must be nonempty and cannot be Question or Section."
            )
        c["s" if section else "q"] = title.strip()
        if not section:
            if c.get("t") not in {"Text", "Choice", "Upload"}:
                raise FormsError(
                    "invalid_spec", "Supported question types are Text, Choice and Upload."
                )
            for flag in ("req", "num", "long"):
                if flag in c and type(c[flag]) is not bool:
                    raise FormsError("invalid_spec", f"{flag} must be a boolean.")
            if c["t"] != "Text" and any(k in c for k in ("num", "long")):
                raise FormsError("invalid_spec", "num and long apply only to Text.")
            if c["t"] == "Choice":
                if (
                    not isinstance(c.get("opts"), list)
                    or not c["opts"]
                    or any(not isinstance(x, str) or not x.strip() for x in c["opts"])
                    or len(set(c["opts"])) != len(c["opts"])
                ):
                    raise FormsError(
                        "invalid_spec", "Choice requires distinct nonempty opts strings."
                    )
            elif "opts" in c:
                raise FormsError("invalid_spec", "opts applies only to Choice.")
            if "mb" in c and (c["t"] != "Upload" or type(c["mb"]) is not int or c["mb"] <= 0):
                raise FormsError("invalid_spec", "mb must be a positive integer on Upload.")
            if "k" in c:
                if not isinstance(c["k"], str) or not c["k"] or c["k"] in keys:
                    raise FormsError("invalid_spec", "Canonical keys must be nonempty and unique.")
                keys.add(c["k"])
        result.append(c)
    return result
