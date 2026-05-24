"""Generate Xianyu listing titles and bilingual description bodies.

Purely template-driven — no LLM calls, no per-card cost. Title: Chinese card
name + variant treatment + finish marker + Chinese set name, kept to ≤30
Chinese characters (Xianyu's practical limit). Body: bilingual block with
condition, finish, set name in both languages plus set code, collector number,
English card name, and a sbwsz data attribution line. Writes listings.json.
"""


def main() -> None:
    raise NotImplementedError()
