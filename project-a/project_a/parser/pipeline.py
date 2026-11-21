"""parse(query) -> ParseResult; answer(result, answers) -> ParseResult (re-decided, no LLM call).

The LLM extracts; normalise.decide() decides. The hand-coded engine is called only on "compute".
"""
from project_a.core.schema import TaxInput
from project_a.engines.hand_coded import HandCodedEngine
from project_a.parser import llm_parse
from project_a.parser.normalise import apply_answers, decide, questions_for
from project_a.parser.schema_out import ParseResult


def _finish(ext, query, raw, llm_meta, engine):
    d = decide(ext)
    pr = ParseResult(status=d["status"], extraction=ext, trace=d["trace"], raw_output=raw,
                     llm=llm_meta, query=query, unsupported_features=d["unsupported"])
    if d["status"] == "ask":
        pr.questions = questions_for(d["missing"], ext.ambiguities)
    elif d["status"] == "compute":
        pr.normalised_input = d["normalised_input"]
        res = (engine or HandCodedEngine()).calculate(TaxInput.from_dict(pr.normalised_input))
        pr.result = {"old_tax": res.old.tax, "new_tax": res.new.tax, "old_total_income": res.old.total_income,
                     "new_total_income": res.new.total_income, "best": res.best, "saving": res.saving,
                     "reason": res.reason, "rule_version": res.rule_version}
        pr.trace.append("engine %s: old %d, new %d, best %s" % (res.engine, res.old.tax, res.new.tax, res.best))
    return pr


def parse(query, client=None, version=None, engine=None):
    if client is None:
        from project_a.llm.client import GeminiClient
        client = GeminiClient(role="parser")
    ext, raw, meta = llm_parse.extract(query, client, version)
    if ext is None:
        return ParseResult(status="parse_error", raw_output=raw, llm=meta, query=query,
                           trace=["LLM output rejected twice: %s" % meta["problems"]])
    return _finish(ext, query, raw, meta, engine)


def answer(result, answers, engine=None):
    """answers: {question id: typed answer}. Applies them to the extraction and decides again."""
    if result.extraction is None:
        raise ValueError("nothing to answer: status %s" % result.status)
    ext = apply_answers(result.extraction, answers)
    pr = _finish(ext, result.query, result.raw_output, result.llm, engine)
    pr.trace = ["answers: %s" % answers] + pr.trace
    return pr
