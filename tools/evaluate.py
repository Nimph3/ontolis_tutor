"""Оценить распознавание терминов на размеченных вопросах. Запуск: python tools/evaluate.py"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import ontology  # noqa: E402


def evaluate(cases, graph):
    tp = fp = fn = correct = 0
    details = []
    for case in cases:
        answer = graph.matched_node(case["question"])
        expected = case["expected_id"]
        correct += answer == expected
        if expected is not None:
            if answer == expected:
                tp += 1
            else:
                fn += 1
                if answer is not None:
                    fp += 1
        elif answer is not None:
            fp += 1
        details.append({"question": case["question"], "expected": expected, "found": answer,
                        "ok": answer == expected})
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    return {"count": len(cases), "accuracy": round(correct / len(cases), 4),
            "precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0,
            "errors": [item for item in details if not item["ok"]]}


if __name__ == "__main__":
    file = ROOT / "tests" / "query_cases.json"
    cases = json.loads(file.read_text(encoding="utf-8"))
    report = evaluate(cases, ontology)
    print(json.dumps(report, indent=2, ensure_ascii=False))
