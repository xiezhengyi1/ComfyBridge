"""提示词安全过滤。

分层：
1. sanitize()     —— 清洗：剥离控制字符、截断长度
2. normalize()    —— 归一化：去所有空白与标点（防“裸 体”“裸.体”拆字绕过），小写
3. check_prompt() —— 规则判断：高危词直接拒绝；普通敏感词按阈值；上下文正则补充
4. llm_moderate() —— 可选 AI 内容审核（config.json 配置 llm 后启用），
   规则引擎无法覆盖语义绕过（同义改写、隐喻），LLM 审核是兜底。

自定义词库：编辑同目录 blocklist.json（{"hard": [...], "soft": [...], "patterns": [[正则, 类别, 是否高危], ...]}）。
"""
import json
import re
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent

# ---------------- 内建词库（会与 blocklist.json 合并） ----------------
# 普通敏感词：按类别组织，命中计数参与阈值判断
SOFT_WORDS = {
    "violence": [
        "暴力", "血腥", "虐待", "酷刑", "行凶", "凶杀", "殴打", "群殴", "斗殴", "杀戮",
        "残杀", "屠杀", "血洗", "砍人", "捅人", "枪击", "持刀", "袭击", "劫持", "绑架",
        "撕票", "灭口", "行刑", "处决", "私刑", "打砸", "纵火", "报复", "复仇", "格斗",
        "搏斗", "肉搏", "打斗", "决斗", "比武", "fight", "violence", "killing",
    ],
    "gore": [
        "血肉模糊", "内脏", "肠子", "脑浆", "断肢", "残肢", "尸块", "腐尸", "碎肉",
        "血浆", "白骨", "尸骸", "开膛", "掏心", "剥皮", "人肉", "解剖", "截肢", "断头",
        "gore", "bloody", "dismember",
    ],
    "sexual": [
        # 基础
        "色情", "情色", "擦边", "软色情", "大尺度", "香艳", "淫荡", "淫秽", "下流",
        "性交", "做爱", "口交", "肛交", "自慰", "手淫", "嫖娼", "卖淫", "援交",
        "下体", "私处", "生殖器", "阴茎", "阴道", "乳", "胸", "臀部特写",
        # 裸体类同义/拆词（高危词兜不住时计入阈值）
        "裸", "裸眼", "裸身", "裸聊", "脱衣", "脱光", "走光", "露点", "露出",
        "湿身", "透视装", "透视", "真空", "情趣", "内衣", "比基尼", "泳装", "肚兜",
        "低胸", "深v", "露脐", "露背", "偷拍", "裙底",
        # 英文
        "porn", "nude", "naked", "sex", "sexy", "erotic", "lewd", "lingerie",
        "bikini", "topless", "nsfw", "hentai", "ecchi", "undressed", "busty",
    ],
    "hate": [
        "支那", "尼哥", "黑鬼", "白皮猪", "犹太猪", "贱民", "劣等民族", "种族清洗",
        "排华", "仇视", "歧视", "辱骂", "贬低", "hate", "nigger", "racist",
    ],
    "self_harm": [
        "自杀", "自残", "割腕", "跳楼", "上吊", "服毒", "轻生", "厌世", "自我了断",
        "不想活", "活不下去", "suicide", "self-harm",
    ],
    "drugs": [
        "毒品", "冰毒", "海洛因", "可卡因", "摇头丸", "迷药", "制毒", "吸毒", "贩毒",
        "鸦片", "吗啡", "笑气", "催情药", "迷奸药", "drugs", "cocaine", "heroin", "meth",
    ],
    "weapons": [
        "枪支", "手枪", "步枪", "狙击枪", "ak47", "冲锋枪", "手雷", "炸弹", "炸药",
        "雷管", "燃烧弹", "爆炸物", "枪械", "弹药", "武器制造", "gun", "weapon", "bomb",
        "explosive",
    ],
    "fraud": [
        "诈骗", "电信诈骗", "杀猪盘", "洗钱", "假钞", "盗刷", "木马", "勒索病毒",
        "黑客攻击", "入侵系统", "贩卖个人信息", "人肉搜索", "开盒", "钓鱼链接", "冒充",
        "scam", "fraud", "phishing", "hack",
    ],
    "terror": [
        "圣战", "极端组织", "恐怖主义", "自杀式袭击", "人弹", "独狼袭击", "isis",
        "terror", "jihad",
    ],
}

# 高危词：命中任意一个（>= hard_reject_threshold）直接拒绝
HARD_WORDS = [
    "杀人", "谋杀", "分尸", "肢解", "斩首", "割喉", "活埋", "凌迟", "碎尸", "灭门",
    "强奸", "轮奸", "幼女", "恋童", "乱伦", "嫖宿幼女",
    "自杀", "自残", "割腕", "轻生",
    "制毒", "海洛因", "冰毒", "迷奸药",
    "炸弹制作", "炸药配方", "土制炸弹", "自制炸弹", "爆炸袭击", "恐怖袭击", "人肉炸弹",
    "开盒", "人肉搜索", "洗钱", "诈骗",
    # ---- 裸体/色情（含同义与英文，防止绕过） ----
    "裸体", "全裸", "半裸", "裸照", "裸女", "裸男", "裸图", "色图", "涩图", "黄图",
    "黄色图片", "艳照", "成人内容", "成人电影", "成人影片", "色情图片", "色情视频",
    "一丝不挂", "不着寸缕", "赤身裸体", "赤身", "光着身子", "光着身体", "不穿衣服",
    "没穿衣服", "没有穿衣服", "无衣物", "衣衫尽褪", "袒胸露乳", "露乳", "露奶",
    "露胸", "酥胸", "乳房", "爆乳", "巨乳", "乳沟", "臀部裸露", "裸露下体",
    "性器官", "性行为", "性爱", "交配", "口交", "肛交",
    "nude", "naked", "topless", "nsfw", "porn", "pornographic", "bare-breasted",
    "barebreast", "undressed", "explicit",
]

# 上下文正则：[正则串, 类别, 是否高危]（作用于归一化后的文本）
PATTERNS = [
    (r"怎么(制作|制造|做).{0,6}(炸弹|炸药|毒)", "weapons", True),
    (r"(如何|怎样|教我).{0,8}(杀人|自杀|制毒|造炸弹)", "violence", True),
    (r"未成年.{0,6}(色情|裸|性|淫)", "sexual", True),
    (r"出售.{0,8}(枪支|毒品|假钞|个人信息)", "fraud", True),
    (r"(攻略|教程|指南).{0,6}(杀人|自杀|诈骗|制毒)", "violence", True),
    # ---- 裸体绕过模式：主体 + 裸露状态 ----
    (r"(女性|女人|少女|女孩|美女|female|woman|girl).{0,12}(裸|脱光|赤身|一丝不挂|不着寸缕|光着|不穿|无遮挡|全裸|半裸)", "sexual", True),
    (r"(裸|脱光|赤身|一丝不挂|不着寸缕|全裸|半裸).{0,12}(女性|女人|少女|女孩|美女|female|woman|girl)", "sexual", True),
    (r"(不穿|没穿|没有穿|无).{0,6}(衣服|衣物|内衣|裤子|任何)", "sexual", False),
    (r"(胸|乳|奶|臀|下体|私处).{0,8}(露出|袒露|外露|全露|走光|特写|无遮挡)", "sexual", True),
    (r"露出.{0,6}(胸|乳|奶|臀|下体|私处)", "sexual", True),
    (r"(衣物|布料|遮挡).{0,6}(极少|很少|没有|无|消失)", "sexual", False),
    (r"(nude|naked|topless|lingerie|erotic|nsfw).{0,10}(woman|girl|female|body|figure)", "sexual", True),
]


def _load_user_blocklist() -> dict:
    path = BASE_DIR / "blocklist.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _build_rules():
    user = _load_user_blocklist()
    soft = {}
    for cat, words in SOFT_WORDS.items():
        soft[cat] = [w.lower() for w in words]
    for w in user.get("soft", []):
        soft.setdefault("custom", []).append(str(w).lower())

    hard = [w.lower() for w in HARD_WORDS] + [str(w).lower() for w in user.get("hard", [])]

    patterns = list(PATTERNS)
    for p in user.get("patterns", []):
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            patterns.append((str(p[0]), str(p[1]), bool(p[2]) if len(p) > 2 else False))

    hard_set = set(hard)
    return soft, hard_set, [(re.compile(pat), cat, h) for pat, cat, h in patterns]


_RULES = None


def _rules():
    global _RULES
    if _RULES is None:
        _RULES = _build_rules()
    return _RULES


def sanitize(text: str, max_len: int = 4000) -> str:
    """清洗输入：剥离控制字符（保留换行/制表符），截断长度。"""
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text or "")
    return t[:max_len]


def normalize(text: str) -> str:
    """归一化：小写 + 去所有空白（含全角）与常见标点，防“裸 体”“裸·体”拆字绕过。"""
    t = (text or "").lower()
    t = re.sub(r"[\s\u3000]+", "", t)
    t = re.sub(r"[，。,.!！?？、;；:：·~～\-—_*★☆●○【】\[\]()（）<>《》\"'“”‘’]", "", t)
    return t


def check_prompt(text: str, cfg: dict) -> dict:
    """返回 {"allowed": bool, "reason": str, "category": str|None, "hits": [str]}"""
    soft, hard_set, patterns = _rules()
    t = normalize(text)

    hard_hits = []
    soft_hits = []
    for w in hard_set:
        if w in t:
            hard_hits.append(w)
    for cat, words in soft.items():
        for w in words:
            if w in t:
                soft_hits.append(f"{cat}:{w}")
    for pat, cat, is_hard in patterns:
        if pat.search(t):
            (hard_hits if is_hard else soft_hits).append(f"{cat}:{pat.pattern[:30]}")

    hard_th = int(cfg.get("hard_reject_threshold", 1))
    soft_th = int(cfg.get("soft_reject_threshold", 3))

    if len(hard_hits) >= hard_th:
        return {
            "allowed": False,
            "reason": f"命中高危关键词（{len(hard_hits)} 个）",
            "category": "hard",
            "hits": hard_hits[:20],
        }
    if len(soft_hits) >= soft_th:
        return {
            "allowed": False,
            "reason": f"命中敏感词（{len(soft_hits)} 个，阈值 {soft_th}）",
            "category": "sensitive",
            "hits": soft_hits[:20],
        }
    return {"allowed": True, "reason": "", "category": None, "hits": []}


# ---------------- 可选 AI 内容审核（LLM 语义兜底） ----------------
_MODERATION_SYSTEM = (
    "你是严格但准确的内容安全审核员。判断这条文生图提示词是否违规，"
    "核心标准是：是否以性挑逗/色情或违法犯罪为目的。\n"
    "违规内容：\n"
    "1) 色情裸露：裸体、半裸、露点、露胸露臀、性行为、脱衣撩衣等明确性暗示动作、"
    "内衣/比基尼/泳装的特写与挑逗展示、透视装、湿身诱惑等以性感为目的的描写；\n"
    "2) 暴力血腥、仇恨歧视、自残自杀、毒品、武器制造、违法犯罪。\n"
    "不违规的正常描写（必须放行）：雨夜淋湿头发、海边度假泳装远景、舞蹈艺术、"
    "体育摄影、艺术人体以外的正常生活场景。\n"
    "同义改写、隐喻、拆字、谐音、英文等同属违规。"
    '只输出一个 JSON 对象：{"allowed": true 或 false, "reason": "一句话中文原因"}'
)


def llm_moderate(text: str, llm_cfg: dict):
    """LLM 语义审核。返回 {"allowed": bool, "reason": str}；调用失败返回 None（放行由规则层负责）。"""
    base = llm_cfg.get("base_url", "").rstrip("/")
    if not base.endswith("/chat/completions"):
        base += "/chat/completions"
    last_err = None
    for attempt in range(2):  # 失败重试一次
        try:
            r = requests.post(
                base,
                json={
                    "model": llm_cfg.get("model", "deepseek-chat"),
                    "messages": [
                        {"role": "system", "content": _MODERATION_SYSTEM},
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0,
                    "max_tokens": 200,
                },
                headers={"Authorization": "Bearer " + llm_cfg.get("api_key", "")},
                timeout=40,
            )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                continue
            content = r.json()["choices"][0]["message"]["content"]
            # 剥离可能的 ```json 代码围栏
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S)
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                last_err = f"无法解析 JSON: {content[:200]}"
                continue
            d = json.loads(m.group(0))
            if "allowed" not in d:
                last_err = f"缺少 allowed 字段: {content[:200]}"
                continue
            return {"allowed": bool(d["allowed"]), "reason": str(d.get("reason", ""))[:200]}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    print(f"[moderation] LLM 审核调用失败，回退规则层（{last_err}）")
    return None
