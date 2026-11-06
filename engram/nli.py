"""On-device contradiction check: a small NLI cross-encoder (DeBERTa-v3-small, int8 ONNX).

Dense cosine catches near-copies of a corroborated memory filed under another
subject, but a paraphrase ("the third charging bay is fine to use") lands at
cosine ~0.7, well below the near-copy threshold. NLI reads the two sentences
together and says whether one contradicts the other, whatever the wording.

NLI models are trained on SNLI/MNLI, where both sentences describe the *same
scene*, so two statements about unrelated things ("wifi is strong" / "printer is
low on paper") come out as contradiction ~1.0. The raw score is useless as a
gate on its own. So the judge is two-stage:

  1. topic     does the incoming text entail "This is about <neighbor subject>"?
               (the paraphrase "third charging bay" -> "about dock 3": 0.82-0.98;
               legit seed memories vs corroborated subjects: <= 0.17)
  2. contra    only for pairs on the same topic: P(contradiction), both orders

Runs on onnxruntime + tokenizers (already pulled in by fastembed), no torch.
Only called for a handful of candidate pairs per screened memory.
"""
import json
import os
import re
from functools import lru_cache

import numpy as np

NLI_REPO = os.environ.get("ENGRAM_NLI_REPO", "cross-encoder/nli-deberta-v3-small")
NLI_FILE = os.environ.get("ENGRAM_NLI_FILE", "onnx/model_quint8_avx2.onnx")
ENABLED = os.environ.get("ENGRAM_NLI", "1") != "0"
TOPIC_THRESHOLD = 0.80


@lru_cache(maxsize=1)
def _model():
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    tok = Tokenizer.from_file(hf_hub_download(NLI_REPO, "tokenizer.json"))
    tok.enable_truncation(max_length=256)
    tok.enable_padding()
    with open(hf_hub_download(NLI_REPO, "config.json")) as f:
        labels = {int(k): v.lower() for k, v in json.load(f)["id2label"].items()}
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = min(4, os.cpu_count() or 1)
    session = ort.InferenceSession(hf_hub_download(NLI_REPO, NLI_FILE), opts, providers=["CPUExecutionProvider"])
    return tok, session, {i.name for i in session.get_inputs()}, labels


def probabilities(pairs: list[tuple[str, str]]) -> list[dict]:
    """[(premise, hypothesis)] -> [{"contradiction": p, "entailment": p, "neutral": p}]"""
    if not pairs:
        return []
    tok, session, inputs, labels = _model()
    enc = tok.encode_batch(pairs)
    feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
    if "token_type_ids" in inputs:
        feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
    logits = session.run(None, feed)[0]
    e = np.exp(logits - logits.max(axis=1, keepdims=True))
    p = e / e.sum(axis=1, keepdims=True)
    return [{labels[i]: float(row[i]) for i in labels} for row in p]


def contradiction(a: str, b: str) -> float:
    """Symmetric: NLI is directional, so score both orders and keep the stronger contradiction."""
    fwd, back = probabilities([(a, b), (b, a)])
    return max(fwd["contradiction"], back["contradiction"])


def contradictions(incoming: str, others: list[str]) -> list[float]:
    pairs = [(o, incoming) for o in others] + [(incoming, o) for o in others]
    p = probabilities(pairs)
    n = len(others)
    return [max(p[i]["contradiction"], p[n + i]["contradiction"]) for i in range(n)]


def topic_hypothesis(subject: str) -> str:
    """dock3 -> "This is about dock 3."  charging_station_3 -> "This is about charging station 3." """
    words = re.sub(r"([a-zA-Z])(\d)", r"\1 \2", subject.replace("_", " ").replace("-", " "))
    return f"This is about {words}."


def judge(incoming: str, neighbors: list[tuple[str, str]]) -> list[dict]:
    """neighbors = [(text, subject)] -> [{"topic": P(entail topic), "contradiction": P or None}]"""
    if not neighbors:
        return []
    topic = [p["entailment"] for p in probabilities([(incoming, topic_hypothesis(s)) for _, s in neighbors])]
    out = [{"topic": round(t, 4), "contradiction": None} for t in topic]
    gated = [i for i, t in enumerate(topic) if t >= TOPIC_THRESHOLD]
    for i, c in zip(gated, contradictions(incoming, [neighbors[i][0] for i in gated])):
        out[i]["contradiction"] = round(c, 4)
    return out


def warmup():
    if ENABLED:
        probabilities([("warmup", "warmup")])
