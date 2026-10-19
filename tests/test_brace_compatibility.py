import threading
from types import SimpleNamespace

import pytest

from test_keyboard_output import load_module


class Editor:
    """Small caret-aware editor with optional brace completion and skip-over."""

    def __init__(self, auto_close=True, text="", caret=0):
        self.auto_close = auto_close
        self.text = text
        self.caret = caret
        self.deletes = 0

    def insert(self, char):
        # Mimic editors which skip an adjacent completion, but cannot skip it
        # after Enter has moved it onto another line.
        if char == "}" and self.auto_close and self.text[self.caret:self.caret + 1] == "}":
            self.caret += 1
            return
        self.text = self.text[:self.caret] + char + self.text[self.caret:]
        self.caret += 1
        if char == "{" and self.auto_close:
            self.text = self.text[:self.caret] + "}" + self.text[self.caret:]

    def enter(self):
        self.insert("\n")
        # Brace-aware Enter often leaves a closing brace on the following line.
        if self.text[self.caret:self.caret + 1] == "}":
            self.text = self.text[:self.caret] + "\n" + self.text[self.caret:]

    def delete(self):
        self.deletes += 1
        self.text = self.text[:self.caret] + self.text[self.caret + 1:]


def connect_controller(module, editor):
    controller = module.SendInputController()

    def send(vk, flags, scan=0):
        if flags & controller.KEYEVENTF_KEYUP:
            return
        if flags & controller.KEYEVENTF_UNICODE:
            editor.insert(chr(scan))
        elif vk == controller.VK_DELETE:
            editor.delete()
        elif vk == controller.VK_RETURN:
            editor.enter()

    controller._send_input_event = send
    return controller


NESTED_CODE = "int main() {\n    if (1) {\n        while (0) {\n        }\n    }\n}\n"


def write_code(controller, editor, code, compatible):
    for char in code:
        if char == "\n":
            editor.enter()
        else:
            controller.type_code_char(char, 0, compatible)


def test_old_output_reproduces_extra_right_braces(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    editor = Editor()
    write_code(connect_controller(module, editor), editor, NESTED_CODE, False)
    assert editor.text.count("}") > NESTED_CODE.count("}")


@pytest.mark.parametrize("code", [NESTED_CODE, NESTED_CODE * 200, "{}\n{{}}\n{\n}\n"])
def test_compatibility_preserves_complete_code(monkeypatch, code):
    module = load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    editor = Editor()
    write_code(connect_controller(module, editor), editor, code, True)
    assert editor.text == code
    assert editor.deletes == code.count("{")


def test_plain_mode_never_deletes_existing_text(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    editor = Editor(False, "existing suffix", 0)
    code = "{value}"
    write_code(connect_controller(module, editor), editor, code, False)
    assert editor.text == code + "existing suffix"
    assert editor.deletes == 0


def test_missing_completion_at_end_does_not_remove_source_characters(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    editor = Editor(False)
    code = '// { in comment\nconst char *s = "{text}";\n{}\n'
    write_code(connect_controller(module, editor), editor, code, True)
    assert editor.text == code


def test_delete_waits_for_target_completion(monkeypatch):
    module = load_module()
    editor = Editor(False)
    controller = connect_controller(module, editor)

    def process_completion(delay):
        if delay >= 0.05:
            editor.text = editor.text[:editor.caret] + "}" + editor.text[editor.caret:]

    monkeypatch.setattr(module.time, "sleep", process_completion)
    controller.type_code_char("{", 0, True)
    assert editor.text == "{"


@pytest.mark.parametrize("compatible", [False, True])
def test_gui_worker_uses_selected_mode_for_entire_run(monkeypatch, compatible):
    module = load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    editor = Editor()
    controller = connect_controller(module, editor)
    monkeypatch.setattr(module, "SendInputController", lambda: controller)

    class InlineThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    class Widget:
        def config(self, **kwargs):
            self.state = kwargs.get("state")

    class Progress(dict):
        def step(self, value):
            self["value"] += value

    # after() runs callbacks immediately to exercise the real worker and cleanup.
    monkeypatch.setattr(module.threading, "Thread", InlineThread)
    app = module.KeyboardSimulator.__new__(module.KeyboardSimulator)
    app.simulation_active = False
    app.repetition_entry = SimpleNamespace(get=lambda: "2")
    app.text = SimpleNamespace(get=lambda *args: NESTED_CODE)
    app.delay_var = SimpleNamespace(get=lambda: 0)
    app.interval_var = SimpleNamespace(get=lambda: 0)
    app.newline_mode_var = SimpleNamespace(get=lambda: "普通使用Enter换行")
    app.brace_compat_var = SimpleNamespace(get=lambda: compatible)
    app.clear_text_var = SimpleNamespace(get=lambda: False)
    app.brace_compat_check = Widget()
    app.button_start = Widget()
    app.button_stop = Widget()
    app.stop_event = threading.Event()
    app.progress = Progress()
    app.records = []
    app.window = SimpleNamespace(after=lambda delay, callback, *args: callback(*args))
    app.update_record_text = lambda _: None
    app.report_input_error = lambda error: pytest.fail(str(error))

    app.simulate_input()

    if compatible:
        assert editor.text == NESTED_CODE * 2
    else:
        assert editor.text.count("}") > (NESTED_CODE * 2).count("}")
    assert len(app.records) == 2
    assert app.simulation_active is False
    assert app.brace_compat_check.state == module.NORMAL
