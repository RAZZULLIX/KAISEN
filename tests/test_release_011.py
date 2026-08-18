"""0.1.1-alpha additions: D language, multi-model chat templates,
hardened danger scan, edit-scope extraction, KAI ON/cookie/persistence."""
import pytest

from kaisen.llm import _chat_transcript, _infer_chat_template
from kaisen.skills import extract_function_names, find_dangerous
from kaisen.languages import lang_from_ext, lang_from_goal, normalize_lang, ext_from_lang
from kaisen.kai import KaiSession, _RUNS_FILE
from kaisen.suggest import _preserve_user_baseline


# ----------------------------------------------------------------------
# D language
# ----------------------------------------------------------------------

def test_d_language_registered():
    assert normalize_lang("d") == "d"
    assert normalize_lang("D") == "d"
    assert normalize_lang("dlang") == "d"
    assert ext_from_lang("d") == ".d"
    assert lang_from_ext(".d") == "d"
    assert lang_from_ext("foo.d") == "d"


def test_d_goal_detection():
    assert lang_from_goal("write a d program") == "d"
    assert lang_from_goal("make it in D, please") == "d"


def test_d_danger_patterns():
    assert find_dangerous("import std.process; void main(){}", "d") is not None
    assert find_dangerous("import std.socket; void main(){}", "d") is not None
    assert find_dangerous("module x; void main(){ int i = 0; }", "d") is None


def test_d_function_extraction():
    names = extract_function_names("module x;\nvoid update() {}\nint helper(int x) { return x; }\nvoid main(){}", "d")
    assert names == {"update", "helper", "main"}


# ----------------------------------------------------------------------
# Chat templates
# ----------------------------------------------------------------------

def test_infer_chat_template():
    assert _infer_chat_template("gpt-oss-20b") == "gptoss"
    assert _infer_chat_template("Qwen2.5-Coder-7B") == "qwen"
    assert _infer_chat_template("gemma-3-4b-it") == "gemma"
    assert _infer_chat_template("Llama-3.1-8B") == "llama3"
    assert _infer_chat_template("DeepSeek-R1") == "deepseek"
    assert _infer_chat_template("some-unknown-model") == "chatml"


def test_transcript_chatml_shape():
    out = _chat_transcript([{"role": "user", "content": "hi"}], "chatml")
    assert out.startswith("<|im_start|>system\n") and "<|im_start|>user\nhi<|im_end|>\n" in out
    assert out.endswith("<|im_start|>assistant\n")


def test_transcript_gptoss_keeps_legacy_format():
    out = _chat_transcript([{"role": "user", "content": "hi"}], "gptoss")
    assert out.startswith("<|start|>system<|message|>")
    assert out.endswith("<|start|>assistant")


def test_transcript_llama3_has_bos_once():
    out = _chat_transcript([{"role": "user", "content": "hi"}], "llama3")
    assert out.startswith("<|begin_of_text|><|start_header_id|>system")
    assert out.count("<|begin_of_text|>") == 1
    assert out.endswith("<|start_header_id|>assistant<|end_header_id|>\n\n")


def test_transcript_gemma_folds_system_into_user():
    out = _chat_transcript([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], "gemma")
    assert out.startswith("<bos><start_of_turn>user\nsys\n\nhi<end_of_turn>\n")
    assert out.endswith("<start_of_turn>model\n")


# ----------------------------------------------------------------------
# Hardened danger scan
# ----------------------------------------------------------------------

def test_danger_exec_family():
    for token in ("execl(", "execlp(", "execle(", "execv(", "execvp(", "execvpe(",
                  "fexecve(", "posix_spawn", "posix_spawnp"):
        assert find_dangerous(f"int main(){{{token};}}", "c") is not None


def test_danger_socket_and_dlopen():
    assert find_dangerous("int main(){ int s = socket(1,2,3); }", "c") == "socket("
    assert find_dangerous("int main(){ void*h=dlopen(0,0); }", "c") == "dlopen("


def test_write_mode_fopen_flagged_read_not():
    assert find_dangerous('FILE*f=fopen("/tmp/x","wb");', "c") is not None
    assert find_dangerous('FILE*f=fopen("/tmp/x","r");', "c") is None
    assert find_dangerous('int fd=open("/tmp/x", O_WRONLY|O_CREAT);', "c") is not None
    assert find_dangerous('int fd=open("/tmp/x", O_RDONLY);', "c") is None


# ----------------------------------------------------------------------
# Edit-scope extraction
# ----------------------------------------------------------------------

def test_extract_function_names_c():
    code = "static inline int a(void){}\nvoid b(int x){ return; }\nint main(void){return 0;}"
    assert extract_function_names(code, "c") == {"a", "b", "main"}


def test_extract_function_names_python():
    code = "def a():\n    pass\n\nasync def b():\n    pass\n"
    assert extract_function_names(code, "python") == {"a", "b"}


# ----------------------------------------------------------------------
# KAI ON parsing + run-goal persistence
# ----------------------------------------------------------------------

class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
    def call(self, method, path, body=None, read_timeout=120.0):
        self.calls.append((method, path, body))
        key = (method, path.split("?")[0])
        return self.routes.get(key, {"error": "unscripted"})

def test_parse_on():
    s = KaiSession(FakeClient({}))
    assert s._parse_on("ON kai_bitloop") == "kai_bitloop"
    assert s._parse_on("kai_bitloop") is None
    assert s._parse_on("") is None


def test_stop_on_targets_other_project():
    routes = {("POST", "/api/engine/stop"): {"ok": True}}
    s = KaiSession(FakeClient(routes))
    s.project = "md5-speed"
    out = s.cmd_stop("ON kai_bitloop")
    assert "kai_bitloop" in out
    body = next(c for c in s.client.calls if c[0:2] == ("POST", "/api/engine/stop"))[2]
    assert body == {"project_id": "kai_bitloop"}


def test_smoke_on_targets_other_project():
    routes = {("POST", "/api/projects/kai_bitloop/smoke"): {"ok": True, "metrics": {}}}
    s = KaiSession(FakeClient(routes))
    s.project = "md5-speed"
    out = s.cmd_smoke("ON kai_bitloop")
    assert "OK" in out
    assert ("POST", "/api/projects/kai_bitloop/smoke") in [c[:2] for c in s.client.calls]


def test_run_goal_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr("kaisen.kai._RUNS_FILE", tmp_path / "kai_runs.json")
    s = KaiSession(FakeClient({}))
    s._run_goal = {"pid": "x", "gen_target": 5, "ts_deadline": None,
                   "start_gen": 0, "start_hist": 0, "start_best": None}
    s._save_run_goal()
    s2 = KaiSession(FakeClient({}))
    assert s2._run_goal == {"pid": "x", "gen_target": 5, "ts_deadline": None,
                            "start_gen": 0, "start_hist": 0, "start_best": None}
    s2._run_goal = None
    s2._save_run_goal()
    assert KaiSession(FakeClient({}))._run_goal is None


# ----------------------------------------------------------------------
# Suggest: user baseline preservation
# ----------------------------------------------------------------------

def test_preserve_user_baseline_overrides_llm_rewrite():
    spec = {
        "name": "x",
        "data": {"baseline_source": "original.c"},
        "files": {"original.c": "AI GENERATED", "harness/build.py": "# build"},
    }
    out = _preserve_user_baseline(spec, "original.c", "USER EXACT CODE")
    assert out["files"]["original.c"] == "USER EXACT CODE"
    assert out["files"]["harness/build.py"] == "# build"
    assert out["data"]["baseline_source"] == "original.c"


def test_preserve_user_baseline_adds_missing_key():
    spec = {"name": "x", "files": {"harness/build.py": "# build"}}
    out = _preserve_user_baseline(spec, "original.d", "USER EXACT CODE")
    assert out["files"]["original.d"] == "USER EXACT CODE"
    assert out["data"]["baseline_source"] == "original.d"
