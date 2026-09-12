"""Tsi 助手本地评测系统的稳定公共入口。"""

from app.evaluation.contracts import EvaluationCase, EvaluationSuite, load_suite

__all__ = ["EvaluationCase", "EvaluationSuite", "load_suite"]
