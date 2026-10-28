"""Validate the existing declarative spec without importing the reference project."""

from copy import deepcopy

from .errors import FormsError

TYPES = {
    "Text": "Question.TextField",
    "Choice": "Question.Choice",
    "Upload": "Question.FileUpload",
    "Rating": "Question.Rating",
    "Date": "Question.DateTime",
    "Ranking": "Question.Ranking",
    "NPS": "Question.NPS",
    "Section": "Question.ColumnGroup",
}

# Measured 2026-10-27: the designer writes RatingShape as its UI label without spaces.
SHAPES = {
    "Star",
    "Number",
    "Heart",
    "ThumbLike",
    "SmileFace",
    "Flag",
    "Lightbulb",
}
OPTION_TYPES = {"Choice", "Ranking"}


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
        allowed = (
            {"s"}
            if section
            else {
                "k",
                "t",
                "q",
                "req",
                "num",
                "long",
                "opts",
                "mb",
                "levels",
                "shape",
                "left",
                "right",
            }
        )
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
            kind = c.get("t")
            if not isinstance(kind, str) or kind not in TYPES or kind == "Section":
                raise FormsError(
                    "invalid_spec",
                    "Supported question types are "
                    + ", ".join(t for t in TYPES if t != "Section")
                    + ".",
                )
            for flag in ("req", "num", "long"):
                if flag in c and type(c[flag]) is not bool:
                    raise FormsError("invalid_spec", f"{flag} must be a boolean.")
            if kind != "Text" and any(k in c for k in ("num", "long")):
                raise FormsError("invalid_spec", "num and long apply only to Text.")
            if kind in OPTION_TYPES:
                if (
                    not isinstance(c.get("opts"), list)
                    or not c["opts"]
                    or any(not isinstance(x, str) or not x.strip() for x in c["opts"])
                    or len(set(c["opts"])) != len(c["opts"])
                ):
                    raise FormsError(
                        "invalid_spec", f"{kind} requires distinct nonempty opts strings."
                    )
            elif "opts" in c:
                raise FormsError("invalid_spec", "opts applies only to Choice and Ranking.")
            if "mb" in c and (kind != "Upload" or type(c["mb"]) is not int or c["mb"] <= 0):
                raise FormsError("invalid_spec", "mb must be a positive integer on Upload.")
            if kind == "Rating":
                levels = c.get("levels", 5)
                if type(levels) is not int or not 2 <= levels <= 10:
                    raise FormsError(
                        "invalid_spec", "Rating levels must be an integer from 2 to 10."
                    )
                if (
                    not isinstance(c.get("shape", "Star"), str)
                    or c.get("shape", "Star") not in SHAPES
                ):
                    raise FormsError(
                        "invalid_spec", "shape must be one of " + ", ".join(sorted(SHAPES)) + "."
                    )
            elif any(k in c for k in ("levels", "shape")):
                raise FormsError("invalid_spec", "levels and shape apply only to Rating.")
            if kind in {"Rating", "NPS"}:
                if any(k in c and not isinstance(c[k], str) for k in ("left", "right")):
                    raise FormsError("invalid_spec", "left and right must be strings.")
            elif any(k in c for k in ("left", "right")):
                raise FormsError("invalid_spec", "left and right apply only to Rating and NPS.")
            if "k" in c:
                if not isinstance(c["k"], str) or not c["k"] or c["k"] in keys:
                    raise FormsError("invalid_spec", "Canonical keys must be nonempty and unique.")
                keys.add(c["k"])
        result.append(c)
    return result
