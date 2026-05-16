#!/usr/bin/env python3
"""
transliterate_lyrics.py
=======================
Detects Hindi (Devanagari) and Urdu (Nastaliq / Arabic-script) in lyrics
and converts them to Hinglish – a colloquial Roman transliteration of the
kind you'd read on WhatsApp: "aap ka ye kehna banta hai do na".

Public API
----------
    detect_script(text)          → 'hindi' | 'urdu' | 'latin'
    to_hinglish(text)            → transliterated str  |  None (already Latin)
    transliterate_if_needed(text)→ always returns str
"""

import re
import unicodedata
from typing import Optional

# ── Unicode range regexes ─────────────────────────────────────────────────────
_RE_DEV  = re.compile(r'[\u0900-\u097F]')
_RE_ARAB = re.compile(r'[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]')


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Script detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_script(text: str) -> str:
    """Return 'hindi', 'urdu', or 'latin' based on dominant script."""
    non_space = [c for c in text if not c.isspace()]
    if not non_space:
        return 'latin'
    total = len(non_space)
    dev  = sum(1 for c in non_space if _RE_DEV.match(c))
    arab = sum(1 for c in non_space if _RE_ARAB.match(c))
    if dev  / total > 0.15:
        return 'hindi'
    if arab / total > 0.15:
        return 'urdu'
    return 'latin'


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Devanagari → Hinglish
# ─────────────────────────────────────────────────────────────────────────────

def _hindi_to_hinglish(text: str) -> str:
    """Use indic_transliteration if available, else fall back to char map."""
    try:
        from indic_transliteration import sanscript
        from indic_transliteration.sanscript import transliterate
        # ITRANS is the closest scheme to colloquial Hinglish
        roman = transliterate(text, sanscript.DEVANAGARI, sanscript.ITRANS)
        return _simplify_itrans(roman)
    except ImportError:
        return _devanagari_basic(text)


# ITRANS → colloquial Hinglish post-processing table
# Order matters: apply multi-char patterns before single-char ones.
_ITRANS_SUBS = [
    # Long vowels → short (aa→a makes "aap" not "aaap")
    ('aa', 'a'), ('ii', 'i'), ('uu', 'u'),
    # Retroflex → plain ASCII
    ('T', 't'), ('D', 'd'), ('N', 'n'), ('L', 'l'),
    # Sibilants
    ('Sh', 'sh'), ('sh', 'sh'), ('S', 's'),
    # Nasal / aspirate markers
    ('M', 'n'), ('~', ''), ('H', ''),
    # Separators / punctuation
    ('|', ' '), ('.', '.'),
]

def _simplify_itrans(text: str) -> str:
    result = text
    for old, new in _ITRANS_SUBS:
        result = result.replace(old, new)
    result = re.sub(r' {2,}', ' ', result)
    return result.strip()


# Minimal Devanagari char-map for environments without indic_transliteration
_DEV_MAP: dict[str, str] = {
    # Independent vowels
    'अ':'a',  'आ':'aa', 'इ':'i',  'ई':'ee', 'उ':'u',  'ऊ':'oo',
    'ए':'e',  'ऐ':'ai', 'ओ':'o',  'औ':'au', 'ऋ':'ri', 'ॠ':'ri',
    # Vowel signs (matras)
    'ा':'a',  'ि':'i',  'ी':'ee', 'ु':'u',  'ू':'oo',
    'े':'e',  'ै':'ai', 'ो':'o',  'ौ':'au', 'ृ':'ri',
    # Nasalisation / anusvara / visarga
    'ं':'n',  'ँ':'n',  'ः':'h',
    # Virama (halant) – suppresses inherent vowel
    '्':'',
    # Consonants
    'क':'k',  'ख':'kh', 'ग':'g',  'घ':'gh', 'ङ':'ng',
    'च':'ch', 'छ':'chh','ज':'j',  'झ':'jh', 'ञ':'ny',
    'ट':'t',  'ठ':'th', 'ड':'d',  'ढ':'dh', 'ण':'n',
    'त':'t',  'थ':'th', 'द':'d',  'ध':'dh', 'न':'n',
    'प':'p',  'फ':'ph', 'ब':'b',  'भ':'bh', 'म':'m',
    'य':'y',  'र':'r',  'ल':'l',  'व':'v',
    'श':'sh', 'ष':'sh', 'स':'s',  'ह':'h',
    'ळ':'l',
    # Nukta consonants (Perso-Arabic borrowings used in Hindi)
    'क़':'q', 'ख़':'kh','ग़':'gh','ज़':'z',
    'ड़':'r', 'ढ़':'rh','फ़':'f', 'य़':'y',
    # Devanagari digits
    '०':'0','१':'1','२':'2','३':'3','४':'4',
    '५':'5','६':'6','७':'7','८':'8','९':'9',
    # Punctuation
    '।':'.',  '॥':'. ', ' ':' ', '\n':'\n', '\t':'\t',
}

def _devanagari_basic(text: str) -> str:
    """Character-by-character Devanagari → Roman (fallback, no library needed)."""
    out = []
    for ch in text:
        if ch in _DEV_MAP:
            out.append(_DEV_MAP[ch])
        elif ch.isascii():
            out.append(ch)
        elif unicodedata.category(ch)[0] in ('P', 'Z', 'N'):
            out.append(ch)
        # else: skip unmapped Devanagari
    return ''.join(out)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Urdu (Nastaliq / Arabic script) → Hinglish
# ─────────────────────────────────────────────────────────────────────────────
# Urdu is an abjad: short vowels are usually not written.  We do the best
# consonantal mapping possible; vowel marks (harakat) are handled when present.

_URDU_MAP: dict[str, str] = {
    # ── Basic Arabic / Urdu letters ──────────────────────────────────────────
    '\u0627': 'a',    # ا  alef
    '\u0622': 'aa',   # آ  alef madda
    '\u0623': 'a',    # أ  alef + hamza above
    '\u0625': 'i',    # إ  alef + hamza below
    '\u0628': 'b',    # ب  be
    '\u067E': 'p',    # پ  pe
    '\u062A': 't',    # ت  te
    '\u0679': 'tt',   # ٹ  tte (retroflex)
    '\u062B': 's',    # ث  se
    '\u062C': 'j',    # ج  jeem
    '\u0686': 'ch',   # چ  che
    '\u062D': 'h',    # ح  bari he
    '\u062E': 'kh',   # خ  khe
    '\u062F': 'd',    # د  dal
    '\u0688': 'dd',   # ڈ  ddal (retroflex)
    '\u0630': 'z',    # ذ  zal
    '\u0631': 'r',    # ر  re
    '\u0691': 'rr',   # ڑ  rre (retroflex)
    '\u0632': 'z',    # ز  ze
    '\u0698': 'zh',   # ژ  zhe
    '\u0633': 's',    # س  seen
    '\u0634': 'sh',   # ش  sheen
    '\u0635': 's',    # ص  suad
    '\u0636': 'z',    # ض  zuad
    '\u0637': 't',    # ط  toe
    '\u0638': 'z',    # ظ  zoe
    '\u0639': '',     # ع  ain  (silent in colloquial Urdu)
    '\u063A': 'gh',   # غ  ghain
    '\u0641': 'f',    # ف  fe
    '\u0642': 'q',    # ق  qaaf
    '\u06A9': 'k',    # ک  kaaf (Urdu form)
    '\u0643': 'k',    # ك  kaaf (Arabic form)
    '\u06AF': 'g',    # گ  gaaf
    '\u0644': 'l',    # ل  laam
    '\u0645': 'm',    # م  meem
    '\u0646': 'n',    # ن  noon
    '\u06BA': 'n',    # ں  noon ghunna (nasal)
    '\u0648': 'w',    # و  wao
    '\u06C1': 'h',    # ہ  choti he (Urdu he)
    '\u06BE': 'h',    # ھ  do chashmi he
    '\u0647': 'h',    # ه  Arabic he
    '\u06C3': 'at',   # ۃ  te marbuta
    '\u0621': '',     # ء  hamza (often silent)
    '\u0626': 'y',    # ئ  ye with hamza
    '\u0624': 'w',    # ؤ  wao with hamza
    '\u06CC': 'y',    # ی  ye (Urdu)
    '\u06D2': 'e',    # ے  ye (final, Urdu)
    '\u0649': 'a',    # ى  alef maqsura
    # ── Vowel marks (harakat – rare in Urdu prose) ───────────────────────────
    '\u064E': 'a',    # fatha   → a
    '\u064F': 'u',    # damma   → u
    '\u0650': 'i',    # kasra   → i
    '\u064B': 'an',   # tanwin fath
    '\u064C': 'un',   # tanwin damm
    '\u064D': 'in',   # tanwin kasr
    '\u0651': '',     # shadda  (gemination marker, skip)
    '\u0652': '',     # sukun   (zero vowel, skip)
    '\u0654': '',     # hamza above
    '\u0655': '',     # hamza below
    # ── Punctuation ───────────────────────────────────────────────────────────
    '\u060C': ',',    # ،  Urdu comma
    '\u06D4': '.',    # ۔  Urdu full stop
    '\u061F': '?',    # ؟  Urdu question mark
    '\u06DD': '',     # ۝  end of ayah
    # ── Whitespace ───────────────────────────────────────────────────────────
    ' ': ' ', '\n': '\n', '\r': '\r', '\t': ' ',
}

_ZERO_WIDTH = re.compile(r'[\u200B-\u200F\u202A-\u202E\uFEFF]')

def _urdu_to_hinglish(text: str) -> str:
    # Strip zero-width / directional markers first
    text = _ZERO_WIDTH.sub('', text)
    out = []
    for ch in text:
        if ch in _URDU_MAP:
            out.append(_URDU_MAP[ch])
        elif ch.isascii():
            out.append(ch)
        elif unicodedata.category(ch)[0] in ('P', 'Z', 'N'):
            out.append(ch)
        # else: skip unmapped character
    result = ''.join(out)
    result = re.sub(r' {2,}', ' ', result)       # collapse spaces
    result = re.sub(r"'{2,}", '', result)          # remove doubled apostrophes
    return result.strip()


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Public API
# ─────────────────────────────────────────────────────────────────────────────

def to_hinglish(text: str) -> Optional[str]:
    """
    Transliterate *text* to Hinglish if it is in Hindi or Urdu script.
    Returns ``None`` when the text is already in Latin or an unsupported
    script (so the caller can decide whether to replace or keep original).
    """
    if not text or not text.strip():
        return None
    script = detect_script(text)
    if script == 'hindi':
        return _hindi_to_hinglish(text)
    if script == 'urdu':
        return _urdu_to_hinglish(text)
    return None


def transliterate_if_needed(text: str) -> str:
    """
    Always returns a string: Hinglish if the input was Hindi/Urdu,
    otherwise the original unchanged.
    """
    result = to_hinglish(text)
    return result if result is not None else text