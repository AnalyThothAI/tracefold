"""Pure, shared identity for one exact News atom.

This module owns only deterministic comparison identity.  It does not know
about Story rows, Push delivery state, PostgreSQL, or provider transports.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

import regex

EXACT_ATOM_IDENTITY_VERSION: Final = "news_exact_atom_identity_v1"
NEWS_PUSH_ADMISSION_POLICY_VERSION: Final = "news_push_exact_atom_admission_v1"
OPENNEWS_EXACT_ATOM_HORIZON_MS: Final = 12 * 60 * 60_000
MAX_COMPARISON_CHARS: Final = 500

EventFamily = Literal["market_telemetry", "filing", "disaster", "general"]

_SOURCE_PREFIX_RE = re.compile(
    r"^(?:(?:just\s+in|breaking|update|exclusive|alert|urgent|developing|wsj|reuters|ap|bbc|cnn|cnbc|coindesk)\s*[:|\-–—]\s*)+",
    re.IGNORECASE,
)
_REPLY_PREFIX_RE = re.compile(r"^(?:rt\s+)?@[A-Za-z0-9_]{1,32}\s*:\s*", re.IGNORECASE)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(
    r"(?P<currency>[$€£¥])?\s*(?P<number>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<scale>trillion|billion|million|thousand|tn|bn|[tbmk])?\s*(?P<percent>%)?",
    re.IGNORECASE,
)

# Pinned, code-owned table retained byte-for-byte from Story V2 comparison.
_TRADITIONAL_TO_SIMPLIFIED = str.maketrans(
    {
        pair[0]: pair[1]
        for pair in re.findall(
            r"\S+",
            (
                "與与 國国 銀银 請请 萬万 專专 業业 東东 兩两 嚴严 個个 豐丰 臨临 為为 麗丽 "
                "舉举 義义 烏乌 樂乐 習习 鄉乡 書书 買买 亂乱 爭争 於于 雲云 亞亚 產产 億亿 "
                "僅仅 從从 倉仓 價价 眾众 優优 會会 傳传 傷伤 偉伟 側侧 儲储 兒儿 兌兑 黨党 "
                "關关 興兴 養养 內内 軍军 農农 凍冻 淨净 鳳凤 擊击 劃划 則则 創创 刪删 別别 "
                "劇剧 辦办 務务 動动 勵励 勞劳 勢势 區区 醫医 華华 協协 單单 賣卖 衛卫 廳厅 "
                "歷历 壓压 縣县 參参 雙双 發发 變变 葉叶 號号 聽听 啟启 員员 週周 響响 園园 "
                "圍围 圖图 團团 圓圆 場场 壞坏 塊块 堅坚 報报 塵尘 聲声 處处 備备 復复 頭头 "
                "奪夺 獎奖 婦妇 媽妈 嬰婴 學学 寧宁 實实 審审 寬宽 寶宝 將将 尋寻 對对 導导 "
                "層层 屬属 島岛 峽峡 幣币 師师 帳账 帶带 幫帮 庫库 廢废 廣广 廠厂 張张 強强 "
                "彈弹 歸归 錄录 當当 徹彻 徵征 憶忆 應应 懷怀 態态 總总 戀恋 戲戏 戶户 擔担 "
                "擴扩 擺摆 攔拦 擬拟 擁拥 撥拨 據据 擇择 採采 換换 損损 搶抢 攜携 數数 斷断 "
                "無无 時时 暫暂 術术 機机 殺杀 雜杂 權权 條条 來来 楊杨 極极 構构 槍枪 標标 "
                "樓楼 樹树 樣样 檔档 橋桥 檢检 歐欧 歡欢 歲岁 殘残 氣气 漢汉 溝沟 滅灭 滬沪 "
                "滯滞 滿满 漁渔 濟济 濃浓 濕湿 灣湾 災灾 煉炼 熱热 愛爱 牆墙 狀状 獨独 獲获 "
                "環环 現现 畫画 異异 療疗 盜盗 盤盘 監监 蓋盖 盡尽 礦矿 碼码 禮礼 禍祸 種种 "
                "穩稳 窮穷 競竞 筆笔 築筑 簡简 簽签 糧粮 糾纠 紀纪 約约 紅红 紐纽 級级 納纳 "
                "紙纸 紛纷 終终 組组 結结 絕绝 統统 綁绑 經经 綜综 綠绿 維维 網网 緊紧 練练 "
                "縱纵 績绩 織织 繼继 續续 罷罢 職职 聯联 聰聪 肅肃 腦脑 膽胆 臉脸 臺台 舊旧 "
                "艦舰 藝艺 節节 蘇苏 藍蓝 虛虚 蟲虫 補补 裝装 裡里 製制 複复 襲袭 見见 規规 "
                "覺觉 覽览 觀观 觸触 訂订 計计 訊讯 記记 訓训 訪访 設设 許许 訴诉 診诊 詞词 "
                "試试 話话 該该 詳详 認认 誤误 說说 調调 談谈 諾诺 謀谋 講讲 謝谢 證证 識识 "
                "譜谱 議议 護护 讀读 讓让 負负 財财 責责 賬账 貢贡 貧贫 貨货 販贩 貪贪 資资 "
                "賊贼 賓宾 賞赏 賠赔 賢贤 質质 購购 贈赠 贏赢 趙赵 趕赶 跡迹 踐践 車车 軌轨 "
                "軟软 轉转 輪轮 輸输 轄辖 辭辞 邊边 遼辽 達达 遷迁 過过 運运 還还 這这 進进 "
                "遠远 違违 連连 選选 遺遗 郵邮 鄭郑 釋释 鑒鉴 針针 釣钓 鈔钞 鐘钟 鋼钢 錢钱 "
                "鍋锅 鎖锁 鎮镇 鏡镜 鐵铁 鑰钥 鑽钻 長长 門门 閃闪 閉闭 問问 間间 閣阁 閱阅 "
                "隊队 階阶 際际 陸陆 險险 隱隐 隨随 難难 電电 靈灵 靜静 韓韩 頁页 頂顶 項项 "
                "順顺 須须 預预 頒颁 領领 頻频 題题 額额 顏颜 風风 飛飞 飯饭 飲饮 餘余 館馆 "
                "馬马 駐驻 駕驾 驗验 驚惊 鬥斗 魚鱼 鮮鲜 鳥鸟 鳴鸣 鴻鸿 麥麦 黃黄 點点 龍龙 龜龟"
            ),
        )
    }
)


@dataclass(frozen=True, slots=True)
class NewsExactAtomIdentity:
    comparison_title: str
    comparison_fingerprint: str
    event_family: EventFamily
    duplicate_window_ms: int
    identity_version: str = EXACT_ATOM_IDENTITY_VERSION


def describe_exact_atom(title: str) -> NewsExactAtomIdentity:
    comparison = comparison_title(title)
    family = event_family(comparison)
    return NewsExactAtomIdentity(
        comparison_title=comparison,
        comparison_fingerprint=hashlib.sha256(comparison.encode("utf-8")).hexdigest(),
        event_family=family,
        duplicate_window_ms=min(event_window_ms(family), OPENNEWS_EXACT_ATOM_HORIZON_MS),
    )


def comparison_title(title: str) -> str:
    value = unicodedata.normalize("NFKC", str(title or "")).translate(_TRADITIONAL_TO_SIMPLIFIED)
    value = "".join(" " if unicodedata.category(char).startswith("C") else char for char in value)
    value = _REPLY_PREFIX_RE.sub("", value)
    value = _SOURCE_PREFIX_RE.sub("", value)
    value = _URL_RE.sub(" ", value)
    value = value.strip(" \t\r\n\"'“”‘’[]()")
    value = _NUMBER_RE.sub(_comparison_number, value)
    value = regex.sub(r"[^\p{L}\p{N}_]+", " ", value.casefold())
    return _SPACE_RE.sub(" ", value).strip()[:MAX_COMPARISON_CHARS]


def event_family(comparison: str) -> EventFamily:
    if re.search(r"\b(?:oi|open interest|whale oi ratio)\b", comparison):
        return "market_telemetry"
    if re.search(
        r"\b(?:sec filing|filing|stake|shareholding|listing|delisting|earnings|revenue|guidance)\b",
        comparison,
    ):
        return "filing"
    if re.search(r"\b(?:earthquake|quake|flood|hurricane|wildfire|tsunami|eruption)\b", comparison):
        return "disaster"
    return "general"


def event_window_ms(family: EventFamily) -> int:
    return {
        "market_telemetry": 2 * 60 * 60_000,
        "filing": 72 * 60 * 60_000,
        "disaster": 6 * 60 * 60_000,
        "general": 12 * 60 * 60_000,
    }[family]


def decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    return "0" if text in {"-0", ""} else text


def _comparison_number(match: re.Match[str]) -> str:
    try:
        value = _scaled_decimal(match.group("number"), match.group("scale"))
    except InvalidOperation:
        return match.group(0)
    kind = "pct" if match.group("percent") else _currency_kind(match.group("currency")) or "num"
    return f" {kind}_{decimal_text(value)} "


def _scaled_decimal(number: str, scale: str | None) -> Decimal:
    value = Decimal(number.replace(",", ""))
    multiplier = {
        "k": Decimal(1_000),
        "thousand": Decimal(1_000),
        "m": Decimal(1_000_000),
        "mn": Decimal(1_000_000),
        "million": Decimal(1_000_000),
        "b": Decimal(1_000_000_000),
        "bn": Decimal(1_000_000_000),
        "billion": Decimal(1_000_000_000),
        "t": Decimal(1_000_000_000_000),
        "tn": Decimal(1_000_000_000_000),
        "trillion": Decimal(1_000_000_000_000),
    }.get(str(scale or "").casefold(), Decimal(1))
    return value * multiplier


def _currency_kind(symbol: str | None) -> str | None:
    return {"$": "usd", "€": "eur", "£": "gbp", "¥": "cny"}.get(symbol or "")


__all__ = [
    "EXACT_ATOM_IDENTITY_VERSION",
    "NEWS_PUSH_ADMISSION_POLICY_VERSION",
    "EventFamily",
    "NewsExactAtomIdentity",
    "comparison_title",
    "decimal_text",
    "describe_exact_atom",
    "event_family",
    "event_window_ms",
]
