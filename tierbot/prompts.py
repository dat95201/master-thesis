"""What the models are shown: the first prompt (the same for both tiers) and
the feedback of the conversational tier (Table 5.3 of the proposal)."""

import textwrap
from pathlib import Path

from . import javasrc
from .bugs import workdir

SYSTEM = ("You are an automated program repair tool for Java. "
          "You fix the given methods and reply with code only.")

UNREADABLE = ("Your answer could not be read as the given method(s). "
              "Return the complete fixed method(s) in one Java code block.")


def first_prompt(info: dict, note: str = "") -> str:
    """The faulty methods, a failing test and its error. A handover note, if
    any, goes between them and the final instruction, so that without a note
    the prompt is exactly the one a model gets when it runs alone."""
    n = len(info["buggy_methods"])
    what = "method contains" if n == 1 else f"{n} methods contain"
    methods = "\n\n".join(textwrap.indent(javasrc.dedent(t), "    ")
                          for t in info["buggy_methods"].values())
    trig = info["trigger_tests"][0]
    test = trig["test"].rsplit(".", 1)[-1]
    target = "the complete fixed method" if n == 1 else "all the complete fixed methods"
    middle = f"{note}\n\n" if note else ""
    return (f"The following Java {what} a bug:\n\n{methods}\n\n"
            f"It makes this test fail:\n"
            f"    Test:  {test}\n"
            f"    Error: {trig['error']}\n\n"
            f"{middle}Return {target} in one Java code block.")


def _test_line(info: dict, cls: str, line: int) -> str:
    if not line:
        return ""
    f = Path(workdir(info["bug"], "b")) / info["test_dir"] / (cls.split("$")[0].replace(".", "/") + ".java")
    try:
        return f.read_text(errors="replace").splitlines()[line - 1].strip()
    except (OSError, IndexError):
        return ""


def describe_failure(info: dict, verdict: str, fail: dict, compile_error: str):
    """(feedback for the model, one-line reason) for a rejected candidate."""
    if verdict == "compile_error":
        return (f"Your fix does not compile:\n{compile_error}\nPlease provide a corrected version.",
                f"it does not compile: {compile_error.splitlines()[0][:200] if compile_error else ''}")
    simple = fail["class"].rsplit(".", 1)[-1]
    src = _test_line(info, fail["class"], fail.get("line", 0))
    where = f"  (line {fail['line']}: {src})" if src else ""
    err = f"{fail.get('exc', '').rsplit('.', 1)[-1]}: {fail.get('msg', '')}".rstrip(": ")
    text = (f"Your fix is still incorrect. It fails this test:\n"
            f"    Test:  {simple}::{fail.get('test', '-')}{where}\n"
            f"    Error: {err}\n"
            f"Please provide a different fix.")
    return text, f"test {simple}::{fail.get('test', '-')} fails with {err}"


def repeat_notice(nth: int, why: str) -> str:
    return (f"You already proposed this fix in round {nth} "
            f"(the same code up to formatting, comments and variable names). "
            f"It was rejected: {why}\n"
            f"Please provide a different fix.")
