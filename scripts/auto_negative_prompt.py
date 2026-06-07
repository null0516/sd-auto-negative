"""
Auto Negative Prompt Extension for Stable Diffusion WebUI / Reforge
ポジティブプロンプトのワードに対応して、ネガティブプロンプトを自動挿入する拡張機能

変更点:
  - 有効チェックボックスをアコーディオンの外（上部）に配置
  - not-match モード追加：指定ワードがポジティブに存在しない場合に発動
  - and-match モード追加：複数の条件がすべて合致した場合に発動（AND条件）
  - 論理式モード追加：& | ! () を使って A&(B|C) のような複合条件を記述可能
  - デフォルトに戻すボタンを削除
  - トリガーワードをカンマ区切りで複数指定可、どれかヒットで発動（OR条件）
"""

import gradio as gr
import json
import os
import platform
import re
import subprocess

import modules.scripts as scripts
from modules.ui_components import InputAccordion
from modules.processing import StableDiffusionProcessing

DEFAULT_RULES = [
    {"trigger": "tan",            "negative": "dark-skinned male",                  "enabled": True, "match_mode": "word"},
    {"trigger": "Blindfold",      "negative": "(eyes:1.40)",                       "enabled": True, "match_mode": "word"},
    {"trigger": "sleep",          "negative": "open eyes",                         "enabled": True, "match_mode": "word"},
    {"trigger": "animal girl",    "negative": "(hair band,fake animal ears:1.1)",  "enabled": True, "match_mode": "word"},
    {"trigger": "glowing tattoo", "negative": "(glowing eyes:1.2)",                "enabled": True, "match_mode": "word"},
    {"trigger": "Nurse",          "negative": "(Red Cross:1.4)",                   "enabled": True, "match_mode": "word"},
    {"trigger": "selfie",         "negative": "(camera:1.2)",                      "enabled": True, "match_mode": "word"},
]

EXTENSION_DIR = os.path.dirname(os.path.dirname(__file__))
RULES_FILE = os.path.join(EXTENSION_DIR, "auto_negative_rules.json")
EXAMPLE_RULES_FILE = os.path.join(EXTENSION_DIR, "auto_negative_rules.example.json")


# ─── ファイル操作 ────────────────────────────────────────────────────────────

def load_rules():
    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[AutoNeg] ルール読み込みエラー: {e}")

    if os.path.exists(EXAMPLE_RULES_FILE):
        try:
            with open(EXAMPLE_RULES_FILE, "r", encoding="utf-8") as f:
                rules = json.load(f)
            save_rules(rules)
            return rules
        except Exception as e:
            print(f"[AutoNeg] サンプルルール読み込みエラー: {e}")

    rules = DEFAULT_RULES.copy()
    save_rules(rules)
    return rules


def save_rules(rules):
    try:
        with open(RULES_FILE, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[AutoNeg] ルール保存エラー: {e}")
        return False


def open_rules_file():
    if not os.path.exists(RULES_FILE):
        save_rules(load_rules())
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(RULES_FILE)
        elif system == "Darwin":
            subprocess.Popen(["open", RULES_FILE])
        else:
            subprocess.Popen(["xdg-open", RULES_FILE])
        return f"<span style='color:#4ade80'>✅ ファイルを開きました: <code style='font-size:0.82em'>{RULES_FILE}</code></span>"
    except Exception as e:
        return (
            f"<span style='color:orange'>⚠️ 自動で開けませんでした。手動で開いてください:<br>"
            f"<code style='font-size:0.82em'>{RULES_FILE}</code><br>エラー: {e}</span>"
        )


# ─── タグ正規化（重複判定用） ───────────────────────────────────────────────

def _normalize_tag(tag: str) -> str:
    """
    ネガティブプロンプトのタグを比較用に正規化する。
      - 前後の空白を除去
      - 小文字化
      - 重み記法を剥がす:  (tag) / ((tag)) / [tag] / (tag:1.2) → tag
    LoRA/embedding 等の <lora:name:0.8> はそのまま残す（先頭が括弧ではないため）。
    """
    t = tag.strip().lower()
    # 外側の () / [] を剥がす（複数階層対応）
    while len(t) >= 2 and ((t.startswith("(") and t.endswith(")")) or (t.startswith("[") and t.endswith("]"))):
        t = t[1:-1].strip()
    # 末尾の :数値 を重みとして剥がす（"tag:1.2" → "tag"）
    if ":" in t:
        head, _, tail = t.rpartition(":")
        try:
            float(tail.strip())
            t = head.strip()
        except ValueError:
            pass
    return t


def _split_prompt_parts(prompt: str) -> list[str]:
    parts = []
    current = []
    depth = 0
    for ch in str(prompt or "").replace("、", ",").replace(";", ","):
        if ch in "([{":
            depth += 1
        elif ch in ")]}" and depth > 0:
            depth -= 1
        if (ch == "," or ch in "\r\n") and depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(ch)
    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts


def _format_negative_prompt(prompt: str) -> str:
    return ", ".join(_split_prompt_parts(prompt))


def _existing_negative_tags(current_negative: str) -> set:
    """現在のネガティブプロンプトを正規化済みタグの集合に変換する。"""
    return {
        _normalize_tag(p) for p in _split_prompt_parts(current_negative) if p.strip()
    }


# ─── マッチロジック ──────────────────────────────────────────────────────────

def _base_match(trigger: str, positive_lower: str, match_mode: str) -> bool:
    """word / contains / exact の基本マッチ判定"""
    if match_mode == "word":
        pattern = r'(?<![a-zA-Z_])(?:\d+)?' + re.escape(trigger) + r's?(?![a-zA-Z_])'
        return bool(re.search(pattern, positive_lower))
    elif match_mode == "contains":
        return trigger in positive_lower
    elif match_mode == "exact":
        tokens = [t.strip().lower() for t in positive_lower.split(",")]
        return trigger in tokens
    return False


# ─── 論理式パーサー ──────────────────────────────────────────────────────────
#
# 対応構文:
#   girl & (swimsuit | bikini)   … AND / OR / NOT(!) / 括弧
#   girl, boy                    … カンマ区切り → 後方互換OR
#
# 優先順位（低→高）: | < & < ! < ()
#
# トリガー文字列に & | ! ( ) が含まれていれば「式モード」として解析する。
# それ以外はカンマ区切りOR（従来動作）にフォールバックする。

_EXPR_CHARS = re.compile(r'[&|!()\[\]]')


def _is_expr_mode(trigger_str: str) -> bool:
    return bool(_EXPR_CHARS.search(trigger_str))


def _tokenize(expr: str):
    """式を (type, value) のリストに変換する。
    type: 'WORD' | '&' | '|' | '!' | '(' | ')'
    """
    tokens = []
    i = 0
    expr = expr.strip()
    while i < len(expr):
        c = expr[i]
        if c in ' \t':
            i += 1
        elif c == '&':
            tokens.append(('&', '&')); i += 1
        elif c == '|':
            tokens.append(('|', '|')); i += 1
        elif c in ('!', '~'):
            tokens.append(('!', '!')); i += 1
        elif c == '(':
            tokens.append(('(', '(')); i += 1
        elif c == ')':
            tokens.append((')', ')')); i += 1
        else:
            # ワード: &|!()以外の文字を貪欲に収集
            j = i
            while j < len(expr) and expr[j] not in '&|!()[] \t':
                j += 1
            word = expr[i:j].strip().rstrip(',').strip().lower()
            if word:
                tokens.append(('WORD', word))
            i = j
    return tokens


class _Parser:
    """再帰下降パーサー。優先順位: | < & < ! < ()"""

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def consume(self, expected_type=None):
        tok = self.tokens[self.pos]
        if expected_type and tok[0] != expected_type:
            raise ValueError(f"Expected {expected_type}, got {tok}")
        self.pos += 1
        return tok

    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.peek() and self.peek()[0] == '|':
            self.consume('|')
            right = self.parse_and()
            left = ('OR', left, right)
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.peek() and self.peek()[0] == '&':
            self.consume('&')
            right = self.parse_not()
            left = ('AND', left, right)
        return left

    def parse_not(self):
        if self.peek() and self.peek()[0] == '!':
            self.consume('!')
            operand = self.parse_atom()
            return ('NOT', operand)
        return self.parse_atom()

    def parse_atom(self):
        tok = self.peek()
        if tok is None:
            raise ValueError("Unexpected end of expression")
        if tok[0] == '(':
            self.consume('(')
            node = self.parse_expr()
            self.consume(')')
            return node
        if tok[0] == 'WORD':
            self.consume('WORD')
            return ('WORD', tok[1])
        raise ValueError(f"Unexpected token: {tok}")


def _eval_ast(node, positive_lower: str, match_mode: str) -> bool:
    kind = node[0]
    if kind == 'WORD':
        return _base_match(node[1], positive_lower, match_mode)
    if kind == 'AND':
        return _eval_ast(node[1], positive_lower, match_mode) and _eval_ast(node[2], positive_lower, match_mode)
    if kind == 'OR':
        return _eval_ast(node[1], positive_lower, match_mode) or _eval_ast(node[2], positive_lower, match_mode)
    if kind == 'NOT':
        return not _eval_ast(node[1], positive_lower, match_mode)
    return False


def _match_trigger(trigger_str: str, positive_lower: str, match_mode: str) -> bool:
    """
    トリガー文字列とポジティブプロンプトを照合する。

    式モード（& | ! () を含む場合）:
        girl & (swimsuit | bikini)  → girl があり、かつ swimsuit か bikini がある
        !nsfw & portrait            → nsfw がなく、かつ portrait がある

    従来モード（カンマ区切りのみ）:
        match_mode が not- 始まり → すべて不一致で発動（AND-NOT）
        match_mode が and- 始まり → すべて一致で発動（AND）
        それ以外                  → どれかが一致で発動（OR）
    """
    if _is_expr_mode(trigger_str):
        # 式モード: match_mode から not-/and- プレフィックスを除いてベースモードを使う
        base_mode = re.sub(r'^(not-|and-)', '', match_mode)
        try:
            tokens = _tokenize(trigger_str)
            ast = _Parser(tokens).parse_expr()
            return _eval_ast(ast, positive_lower, base_mode)
        except Exception as e:
            print(f"[AutoNeg] 式パースエラー ({trigger_str!r}): {e}")
            return False

    # 従来モード（後方互換）
    triggers = [t.strip().lower() for t in trigger_str.split(",") if t.strip()]
    is_not_mode = match_mode.startswith("not-")
    is_and_mode = match_mode.startswith("and-")
    base_mode   = re.sub(r'^(not-|and-)', '', match_mode)

    if is_not_mode:
        return all(not _base_match(t, positive_lower, base_mode) for t in triggers)
    elif is_and_mode:
        return all(_base_match(t, positive_lower, base_mode) for t in triggers)
    else:
        return any(_base_match(t, positive_lower, base_mode) for t in triggers)


def apply_rules(positive_prompt: str, current_negative: str, rules: list, enabled: bool) -> str:
    if not enabled or not positive_prompt:
        return current_negative

    positive_lower = positive_prompt.lower()
    additions = []

    # 既存ネガティブをタグ単位で正規化した集合。新規追加分も逐次追加していき、
    # 同一ルール内・複数ルール間の重複も同じ判定基準でブロックする。
    existing_tags = _existing_negative_tags(current_negative)

    for rule in rules:
        if not rule.get("enabled", True):
            continue

        trigger_str  = rule.get("trigger", "").strip()
        negative_add = rule.get("negative", "").strip()
        match_mode   = rule.get("match_mode", "word")

        if not trigger_str or not negative_add:
            continue

        matched = _match_trigger(trigger_str, positive_lower, match_mode)

        if matched:
            for part in _split_prompt_parts(negative_add):
                part = part.strip()
                if not part:
                    continue
                key = _normalize_tag(part)
                if not key or key in existing_tags:
                    continue
                existing_tags.add(key)
                additions.append(part)

    if not additions:
        return current_negative

    unique_additions = additions

    if current_negative.strip():
        return current_negative.rstrip(", ") + ", " + ", ".join(unique_additions)
    return ", ".join(unique_additions)


# ─── Hires negative prompt 適用ユーティリティ ────────────────────────────────

def _as_list(value, n: int):
    if isinstance(value, list):
        return value
    if value is None:
        return [""] * n
    return [str(value)] * n


def _get_prompt_at(values, i: int, fallback: str = "") -> str:
    if isinstance(values, list) and i < len(values):
        return values[i] or fallback
    if isinstance(values, str):
        return values or fallback
    return fallback


def _ensure_len(values, n: int, fill: str = ""):
    values = list(values or [])
    while len(values) < n:
        values.append(fill)
    return values


def _split_safe_prompt_tags(value):
    if value is None:
        return []
    return [x.strip() for x in str(value).split(",") if x.strip()]


def _safe_prompt_control_tags(p: StableDiffusionProcessing, index: int) -> list[str]:
    tags = []
    results = list(getattr(p, "safe_prompt_control_results", []) or [])
    for entry in results:
        if not isinstance(entry, dict):
            continue
        prompt_index = entry.get("prompt_index")
        if prompt_index not in (None, index):
            continue
        data = entry.get("data")
        result = entry.get("result")
        if isinstance(data, dict):
            tags.extend(data.get("metadata_tags") or [])
            tags.extend(data.get("filename_tags") or [])
        else:
            tags.extend(getattr(result, "metadata_tags", []) or [])
            tags.extend(getattr(result, "filename_tags", []) or [])

    if not tags:
        extra = getattr(p, "extra_generation_params", {}) or {}
        tags.extend(_split_safe_prompt_tags(extra.get("SafePrompt Tags")))
        tags.extend(_split_safe_prompt_tags(extra.get("SafePrompt Filename Tags")))

    deduped = []
    seen = set()
    for tag in tags:
        key = str(tag).lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(str(tag))
    return deduped


def _positive_prompt_for_matching(p: StableDiffusionProcessing, index: int, positive: str) -> str:
    tags = _safe_prompt_control_tags(p, index)
    if not tags:
        return positive
    return f"{positive}, {', '.join(tags)}" if positive else ", ".join(tags)


def apply_rules_to_hires_negative(p: StableDiffusionProcessing, rules: list, enabled: bool, log_prefix: str = "hires"):
    """
    Hires negative prompt だけに AutoNeg を反映する。

    重要:
      - Hires positive prompt / all_hr_prompts は一切変更しない。
      - Hires negative が空、または未初期化の環境でも通常 negative をベースに補完する。
      - Hires negative が明示入力されている場合はそれを優先し、そこへ不足分だけ追加する。
      - apply_rules 側で重複チェックするため、process と before_hr_process の両方で呼ばれても二重追加されない。
    """
    if not enabled:
        return

    positive_prompts = getattr(p, "all_prompts", None) or [getattr(p, "prompt", "") or ""]
    n = max(
        len(positive_prompts),
        len(getattr(p, "all_negative_prompts", None) or []),
        len(getattr(p, "all_hr_negative_prompts", None) or []),
        1,
    )

    normal_negs = _as_list(getattr(p, "all_negative_prompts", None), n)
    hr_negs = _ensure_len(getattr(p, "all_hr_negative_prompts", None), n)

    changed = False
    for i in range(n):
        positive = _get_prompt_at(positive_prompts, i, _get_prompt_at(positive_prompts, 0, ""))
        positive = _positive_prompt_for_matching(p, i, positive)
        normal_neg = _get_prompt_at(normal_negs, i, _get_prompt_at(normal_negs, 0, ""))
        current_hr_neg = hr_negs[i] or ""

        # Hires negative が未設定なら、AutoNeg 適用済みの通常 negative をベースにする。
        # 明示入力がある場合は、それをベースにして不足分だけ追加する。
        base_hr_neg = current_hr_neg.strip() or normal_neg
        new_hr_neg = apply_rules(positive, base_hr_neg, rules, enabled)

        if new_hr_neg != current_hr_neg:
            changed = True
            added = new_hr_neg[len(base_hr_neg):].strip(", ") if new_hr_neg.startswith(base_hr_neg) else "updated"
            if added:
                print(f"[AutoNeg] [{log_prefix}:{i}] 追加: {added}")
            hr_negs[i] = new_hr_neg

    if changed:
        p.all_hr_negative_prompts = hr_negs
        p.hr_negative_prompt = hr_negs[0] if hr_negs else getattr(p, "hr_negative_prompt", "")


# ─── テーブル変換ユーティリティ ──────────────────────────────────────────────

def rules_to_table(rules):
    return [
        [r.get("trigger", ""), r.get("negative", ""), r.get("match_mode", "word")]
        for r in rules
    ]


def table_to_rules(table_data):
    new_rules = []
    if table_data is None:
        return new_rules
    rows = table_data.values.tolist() if hasattr(table_data, "values") else table_data
    for row in rows:
        if len(row) >= 4:
            trigger, negative, match_mode = row[1], row[2], row[3]
        elif len(row) >= 3:
            trigger, negative, match_mode = row[0], row[1], row[2]
        else:
            continue
        new_rules.append({
            "enabled":    True,
            "trigger":    str(trigger).strip(),
            "negative":   str(negative).strip(),
            "match_mode": str(match_mode).strip(),
        })
    return new_rules


# ─── 拡張スクリプト本体 ──────────────────────────────────────────────────────

class AutoNegativePromptScript(scripts.Script):

    def __init__(self):
        super().__init__()
        self.rules = load_rules()

    def title(self):
        return "Auto Negative Prompt"

    def show(self, is_img2img):
        return scripts.AlwaysVisible

    def ui(self, is_img2img):
        tab_id = "i2i" if is_img2img else "t2i"
        elem_accordion_id = f"auto_neg_accordion_{tab_id}"

        gr.HTML(f"""
            <style>
            #{elem_accordion_id} .auto-neg-actions,
            #{elem_accordion_id} .auto-neg-rule-form {{
                display: flex;
                gap: 8px;
                align-items: stretch;
            }}
            #{elem_accordion_id} .auto-neg-actions {{ flex-wrap: nowrap; margin-bottom: 8px; }}
            #{elem_accordion_id} .auto-neg-actions > .form {{ flex: 1 1 0; min-width: 0 !important; }}
            #{elem_accordion_id} .auto-neg-actions button {{ white-space: nowrap; min-width: 0 !important; }}
            #{elem_accordion_id} .auto-neg-rule-form {{ align-items: flex-end; margin-top: 8px; }}
            #{elem_accordion_id} .auto-neg-rule-left,
            #{elem_accordion_id} .auto-neg-rule-right {{ flex: 1 1 0; min-width: 0; }}
            #{elem_accordion_id} .auto-neg-rule-left .block,
            #{elem_accordion_id} .auto-neg-rule-right .block {{ margin-bottom: 6px; }}
            #{elem_accordion_id} .auto-neg-rule-right button {{ width: 100%; }}
            #{elem_accordion_id} code,
            #{elem_accordion_id} .auto-neg-no-translate {{ unicode-bidi: isolate; }}
            @media (max-width: 760px) {{
                #{elem_accordion_id} .auto-neg-rule-form {{ flex-direction: column; }}
            }}
            </style>
        """)

        with InputAccordion(True, label="Auto Negative Prompt", elem_id=elem_accordion_id) as enabled:
            with gr.Row(elem_classes=["auto-neg-actions"]):
                open_btn   = gr.Button("Open JSON",   variant="secondary", size="sm")
                reload_btn = gr.Button("Reload JSON", variant="secondary", size="sm")
                save_btn   = gr.Button("Save",        variant="primary",   size="sm")

            status_msg = gr.HTML("")

            rules_display = gr.Dataframe(
                headers=["Trigger", "Negative", "Mode"],
                datatype=["str", "str", "str"],
                value=rules_to_table(self.rules),
                interactive=True,
                col_count=(3, "fixed"),
                wrap=True,
            )

            with gr.Row(elem_classes=["auto-neg-rule-form"]):
                with gr.Column(elem_classes=["auto-neg-rule-left"]):
                    new_trigger = gr.Textbox(
                        label="Trigger",
                        placeholder="Blindfold",
                        lines=1,
                    )
                    new_negative = gr.Textbox(
                        label="Negative",
                        placeholder="eyes",
                        lines=1,
                    )
                with gr.Column(elem_classes=["auto-neg-rule-right"]):
                    new_match_mode = gr.Dropdown(
                        choices=["word", "contains", "exact", "and-word", "and-contains", "and-exact", "not-word", "not-contains", "not-exact"],
                        value="word",
                        label="Mode",
                        elem_classes=["auto-neg-no-translate"],
                    )
                    add_btn = gr.Button("Add Rule", variant="primary", size="sm")

            gr.HTML("""
                <details style="margin-top:8px; font-size:0.84em; color:rgba(120,120,120,0.96);">
                <summary>Match Modes / マッチモード</summary>
                <div style="margin-top:6px; line-height:1.55;">
                    Separate triggers with commas for OR matching. Use <code translate="no">&amp;</code>, <code translate="no">|</code>, <code translate="no">!</code>, and parentheses for expressions.<br>
                    カンマ区切りはOR条件です。式では <code translate="no">&amp;</code> / <code translate="no">|</code> / <code translate="no">!</code> / 括弧が使えます。
                </div>
                <table style="margin-top:6px; border-collapse:collapse; line-height:1.55;">
                    <tr><td style="padding-right:14px;"><code translate="no">word</code></td><td>Word boundary match / 単語境界で一致</td></tr>
                    <tr><td style="padding-right:14px;"><code translate="no">contains</code></td><td>Partial text match / 部分一致</td></tr>
                    <tr><td style="padding-right:14px;"><code translate="no">exact</code></td><td>Exact comma-separated token match / カンマ区切りトークンの完全一致</td></tr>
                    <tr><td style="padding-right:14px;"><code translate="no">and-*</code></td><td>All triggers must match / すべて一致で発動</td></tr>
                    <tr><td style="padding-right:14px;"><code translate="no">not-*</code></td><td>Fires when triggers do not match / 一致しない時に発動</td></tr>
                    <tr><td style="padding-right:14px;"><code translate="no">expression</code></td><td>Use <code translate="no">&amp;</code>, <code translate="no">|</code>, <code translate="no">!</code>, parentheses / 式でAND・OR・NOTを指定</td></tr>
                </table>
                <div style="margin-top:6px;">
                    Example: <code translate="no">landscape &amp; (rain | fog)</code>, <code translate="no">portrait &amp; !smile</code>
                </div>
                </details>
            """)

        # ── イベントハンドラ ──────────────────────────────────────────────

        def add_rule_handler(trigger, negative, match_mode):
            trigger = trigger or ""
            negative = negative or ""
            if not trigger.strip() or not negative.strip():
                return self._table(), "<span style='color:orange'>Trigger and Negative are required.</span>"
            self.rules.append({
                "trigger":    trigger.strip(),
                "negative":   negative.strip(),
                "enabled":    True,
                "match_mode": match_mode,
            })
            return self._table(), "<span style='color:#4ade80'>Rule added. Save to keep changes.</span>"

        def save_handler(table_data):
            self.rules = table_to_rules(table_data)
            if save_rules(self.rules):
                return self._table(), (
                    f"<span style='color:#4ade80'>Saved: "
                    f"<code style='font-size:0.82em'>{RULES_FILE}</code></span>"
                )
            return self._table(), "<span style='color:red'>Save failed.</span>"

        def reload_handler():
            self.rules = load_rules()
            return self._table(), (
                f"<span style='color:#60a5fa'>Reloaded {len(self.rules)} rules.</span>"
            )

        def open_handler():
            return open_rules_file()

        def on_table_change(table_data):
            self.rules = table_to_rules(table_data)

        add_btn.click(fn=add_rule_handler, inputs=[new_trigger, new_negative, new_match_mode], outputs=[rules_display, status_msg])
        save_btn.click(fn=save_handler,    inputs=[rules_display],                              outputs=[rules_display, status_msg])
        reload_btn.click(fn=reload_handler, inputs=[],                                          outputs=[rules_display, status_msg])
        open_btn.click(fn=open_handler,    inputs=[],                                           outputs=[status_msg])
        rules_display.change(fn=on_table_change, inputs=[rules_display],                        outputs=[])

        return [enabled]

    def _table(self):
        return rules_to_table(self.rules)

    def process(self, p: StableDiffusionProcessing, enabled: bool):
        if not enabled:
            return

        self.rules = load_rules()
        self._enabled = enabled  # before_hr_process で参照するために保持

        prompts = getattr(p, "all_prompts", None) or [getattr(p, "prompt", "") or ""]
        negative_prompts = _ensure_len(getattr(p, "all_negative_prompts", None), len(prompts))

        for i in range(len(prompts)):
            positive = _positive_prompt_for_matching(p, i, prompts[i])
            negative = negative_prompts[i] or ""
            new_negative = apply_rules(positive, negative, self.rules, enabled)
            if new_negative != negative:
                negative_prompts[i] = new_negative
                added = new_negative[len(negative):].strip(", ")
                print(f"[AutoNeg] [{i}] 追加: {added}")

        p.all_negative_prompts = negative_prompts
        if negative_prompts:
            p.negative_prompt = negative_prompts[0]

        # Hires negative prompt にも同じ AutoNeg 追加分を反映する。
        # process() 時点で hr 系が未初期化の環境にも対応するため、ここで一度入れ、
        # before_hr_process() でも再適用する。apply_rules は重複追加しない。
        # Hires positive prompt / all_hr_prompts は絶対に変更しない。
        apply_rules_to_hires_negative(p, self.rules, enabled, log_prefix="hires-process")

    def before_hr_process(self, p: StableDiffusionProcessing, *args):
        """hires.fix 直前フック（WebUI / Forge / reForge 差異吸収版）"""
        enabled = bool(args[0]) if args else bool(getattr(self, "_enabled", True))
        if not enabled:
            return

        rules = getattr(self, "rules", None) or load_rules()
        apply_rules_to_hires_negative(p, rules, enabled, log_prefix="hires")
