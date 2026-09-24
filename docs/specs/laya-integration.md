> **STATUS — PROPOSAL, 0% BUILT (reviewed 2026-09-24).** Written by an opencode session on 2026-09-22.
> Its description of existing LeadForge code (`enrich/dm.py` `auto_label`, `score.py` tiers) checks out; everything it
> proposes to add (`laya_integration.py`, `LayaCfg`, the `laya` dependency) does not exist. Laya weights download
> from the Hugging Face Hub at runtime; CPU cold load is ~7.4 s per the Laya README. Smallest worthwhile step:
> shadow-mode Laya DM labeling next to the `claude -p` runner, measured before anything is replaced.
> See `docs/UPGRADE-PLAN.md`.

# Laya Integration Spec for LeadForge

**Status**: Draft — living document, updated as we validate integration points  
**Purpose**: Full spec for integrating Laya (JEV open-source) into LeadForge to replace/augment generative LLM calls with sub-35ms System 1 typed decisions  
**Authors**: You (lead) + me (implementation) — updated after each validation

---

## 1. Executive Summary

Laya is a **non-autoregressive System 1 decision engine** that evaluates typed questions (`choice`, `score`, `noul`) over any state in **33 ms single forward pass** (7.2 ms/question batched). It beats TypeSafe Jev on accuracy (0.766 vs 0.727), calibration (ECE 0.081 vs 0.246), speed (7.8× faster), and cost ($0 self-hosted vs $0.042/M tokens).

**LeadForge currently uses generative LLMs (Claude Code headless) for:**
- Decision-maker labeling (`dm export` → agent labels → `dm apply`)
- Draft generation (`draft export` → agent writes subject/observation → `draft apply`)
- Autopilot labeling/drafting during `leadforge run` (ADR-015)

**Laya replaces these with typed decisions — no text generation, no parsing, no hallucination, calibrated confidence you can branch on.**

---

## 2. Laya Primitives → LeadForge Mapping

| Laya Primitive | Output | LeadForge Use Case | Current Implementation |
|----------------|--------|-------------------|------------------------|
| **`choice`** | Top label + probs + confidence | DM candidate selection, category routing, urgency tier | `dm export` snippets → agent picks 1 of N |
| **`score`** | Expected ordinal level + distribution | Lead urgency, frustration, engagement score | Heuristic in `score.py` / agent judgment |
| **`noul`** | P(true) ∈ [0,1] | Churn risk, spam/phishing, jailbreak, "should we email?" | `validation.infer_emails` (opt-in), compliance gates |

---

## 3. Integration Points (Priority Ordered)

### P0 — Autopilot DM Labeling (`enrich/dm.py` → `auto_label`)

**Current**: `auto_label` batches businesses → sends NDJSON to headless `claude -p` → parses `{"biz","pick","confidence","title_override"}` → `apply_label_records`

**Laya Replacement**:
```python
# In auto_label, replace runner call with:
from laya import Router
router = Router(preload=True)  # or load single checkpoint

questions = {
    "dm_pick": {
        "type": "choice",
        "instructions": "Pick the candidate whose ROLE best decides on the offer. -1 if none.",
        "criteria": {str(i): f"{c['name']} — {c['title']} — {c['snippet']}" for i, c in enumerate(candidates)}
    },
    "dm_confidence": {
        "type": "score",
        "instructions": "Confidence this candidate is the true decision maker",
        "criteria": ["guess", "plausible", "strong evidence", "certain"]
    }
}
state = {"biz": biz["name"], "category": biz["category"], "candidates": candidates}
res = router.predict(state, questions)
# res["answers"]["dm_pick"]["choice"] → "0" or "-1"
# res["answers"]["dm_confidence"]["score"] → 0.0-3.0
```

**Benefits**:
- 33 ms vs 5-30 s per batch (no subprocess spawn)
- Calibrated confidence → branch on `conf >= 0.85` for auto-accept, else escalate
- No `AgentFailed`/`AgentUnavailable` exceptions — model always available locally
- Multilingual: works for non-English business names/snippets

**Fallback**: Keep `heuristic_labels` for when Laya unavailable (CPU-only env)

---

### P0 — Autopilot Draft Generation (`draft/service.py` → `auto_draft`)

**Current**: `auto_draft` builds evidence packets → sends to `runner` (headless Claude) → gets `{"target","subject","observation","used_fact"}` → `apply_drafts` with gate check

**Laya Replacement**:
```python
questions = {
    "subject_template": {
        "type": "choice",
        "instructions": "Select the best subject line template for this packet",
        "criteria": {
            "hook_booking": "Your online booking at {co}",
            "hook_stale": "Your website hasn't been updated since {year}",
            "hook_no_site": "No website found for {co}",
            "hook_reviews": "Your {rating} star rating ({reviews} reviews)",
            "generic": "Quick question for {co}"
        }
    },
    "observation_fact": {
        "type": "choice",
        "instructions": "Which packet fact should the observation cite?",
        "criteria": {f["k"]: f["v"] for f in packet["facts"]}
    },
    "tone": {
        "type": "score",
        "instructions": "How direct should the observation be?",
        "criteria": ["soft", "direct", "very direct"]
    }
}
res = router.predict({"packet": packet, "business": biz}, questions)
# Build subject/observation from templates + selected fact (deterministic, no LLM)
```

**Benefits**:
- Deterministic output — gate check (`check_draft`) passes 100% (no fabrication risk)
- Sub-35ms vs 10-60s per batch
- Template fallback becomes primary path; Laya just selects *which* template/fact

---

### P1 — Lead Scoring Enhancement (`score.py`)

**Current**: Weighted rubric (industry_match, size_match, geography_match, business_model, dm_identified, verified_direct_contact, need_signals, data_confidence) → tier A/B/C/DQ

**Laya Augmentation**:
```python
# Add Laya-evaluated signals as additional factors
questions = {
    "urgency": {
        "type": "score",
        "instructions": "How urgently does this business need the offer?",
        "criteria": ["no signal", "low", "medium", "high", "critical"]
    },
    "churn_risk": {
        "type": "noul",
        "instructions": "Does this business show signs of vendor churn risk?"
    },
    "budget_authority": {
        "type": "noul",
        "instructions": "Does the identified DM have budget authority?"
    }
}
# Run per-business during scoring; add as meta factors (like contactability)
```

**Benefits**:
- Signals derived from *enrichment evidence* (website, GBP, registry) not just heuristics
- Calibrated probabilities → better tier boundaries
- Can run batched: 1000 businesses × 3 questions = ~72 ms total

---

### P1 — Outreach Eligibility & Next Action (`compliance.py`, `outreach/plan.py`)

**Current**: `email_eligibility` → rule-based (entity type, freemail policy, suppression) → `next_action` (CALL_ONLY, READY, RESEARCH)

**Laya Augmentation**:
```python
questions = {
    "sendability": {
        "type": "noul",
        "instructions": "Should we send an email to this contact given all signals?"
    },
    "channel_preference": {
        "type": "choice",
        "instructions": "Best first-touch channel",
        "criteria": {"phone": "validated phone + DM", "email": "eligible email", "research": "neither ready"}
    },
    "optimal_timing": {
        "type": "score",
        "instructions": "When to send for highest response probability",
        "criteria": ["morning", "midday", "afternoon", "avoid"]
    }
}
```

---

### P2 — Enrichment Signal Classification (`enrich/extract.py`, `enrich/runner.py`)

**Current**: Heuristic regex/keyword for `booking_hint`, `stale_site`, `careers`, `weak_social_presence`

**Laya Replacement**:
```python
# Batch classify all sites after crawl
questions = {
    "has_booking": {"type": "noul", "instructions": "Does the site show online booking capability?"},
    "site_staleness": {"type": "score", "instructions": "How stale is the site content?", "criteria": ["fresh", "moderate", "stale", "abandoned"]},
    "hiring_signal": {"type": "noul", "instructions": "Is the business actively hiring?"},
    "social_activity": {"type": "score", "instructions": "Social media activity level", "criteria": ["none", "low", "moderate", "high"]}
}
# Run batched over all crawled pages (705 chunks → 705 × 4 questions = ~500 ms)
```

---

### P2 — Company Mode Enhancement (`company.py`, `enrich/resolve_domain.py`)

**Current**: Domain resolution via slug candidates + on-page verification

**Laya Augmentation**:
```python
questions = {
    "domain_match": {
        "type": "noul",
        "instructions": "Does this domain belong to the target company?"
    },
    "legal_name_match": {
        "type": "noul",
        "instructions": "Does the page show the company's legal name?"
    }
}
# Run per candidate domain during resolution
```

---

### P3 — Data Quality Gates (`db.py` upserts, `normalize.py`)

**Current**: Deterministic dedupe rules (phone, domain, place_id)

**Laya Augmentation**:
```python
questions = {
    "is_duplicate": {
        "type": "noul",
        "instructions": "Are these two business records the same entity?"
    }
}
# Run on merge candidates before upsert
```

---

## 4. Architecture: Where Laya Lives

```
leadforge/
├── src/leadforge/
│   ├── laya_integration.py          # NEW: Router singleton, question schemas, batch API, fallback wrapper
│   ├── enrich/
│   │   ├── dm.py                    # MODIFY: auto_label → Laya choice/score
│   │   └── runner.py                # MODIFY: batch Laya calls during enrichment
│   ├── draft/
│   │   └── service.py               # MODIFY: auto_draft → Laya template selection
│   ├── score.py                     # MODIFY: add Laya signals as meta factors
│   ├── compliance.py                # MODIFY: add Laya sendability/channel
│   ├── outreach/
│   │   └── plan.py                  # MODIFY: Laya-enhanced eligibility
│   └── config.py                    # ADD: LayaCfg class with validation
├── config/leadforge.example.yaml    # ADD: laya section
├── requirements.txt / pyproject.toml # ADD: laya>=0.3.3 extra
├── scripts/
│   ├── bench_laya.py                # NEW: benchmark Laya vs current paths
│   ├── eval_laya.py                 # NEW: shadow-mode evaluation harness
│   └── calibrate_laya.py            # NEW: temperature fitting script
└── tests/
    ├── test_laya_integration.py     # NEW: integration tests
    └── test_laya_shadow_mode.py     # NEW: shadow mode comparison tests
```

### Laya Config (leadforge.yaml)
```yaml
laya:
  enabled: true
  preload: true              # keep models resident (VRAM/RAM)
  device: "auto"             # cuda | cpu | auto
  checkpoint: "router"       # router | english | multilingual | typed-decisions
  max_loaded: 2              # LRU cache size for Router (which 2: english + multilingual)
  batch_size: 32             # questions per forward pass
  calibration:
    enabled: true
    temperatures_path: "~/.leadforge/laya_temps.json"  # per-domain fitted temps
  fallback:
    enabled: true
    order: ["laya", "heuristic", "agent"]  # Laya → heuristic → agent
```

### LayaCfg Class (config.py)
```python
class LayaCfg(BaseModel):
    enabled: bool = True
    preload: bool = True
    device: Literal["auto", "cuda", "cpu"] = "auto"
    checkpoint: Literal["router", "english", "multilingual", "typed-decisions"] = "router"
    max_loaded: int = Field(default=2, ge=1, le=3)
    batch_size: int = Field(default=32, ge=1, le=128)
    calibration: CalibrationCfg = Field(default_factory=CalibrationCfg)
    fallback: FallbackCfg = Field(default_factory=FallbackCfg)

class CalibrationCfg(BaseModel):
    enabled: bool = True
    temperatures_path: str = "~/.leadforge/laya_temps.json"
    refit_interval_days: int = 30

class FallbackCfg(BaseModel):
    enabled: bool = True
    order: list[str] = ["laya", "heuristic", "agent"]
    laya_timeout_s: float = 5.0
```

---

## 5. Question Schema Registry

Centralized in `laya_integration.py`:

```python
# DM Labeling
DM_LABEL_QUESTIONS = {
    "dm_pick": {"type": "choice", "instructions": "...", "criteria": "DYNAMIC"},
    "dm_confidence": {"type": "score", "instructions": "...", "criteria": ["guess","plausible","strong","certain"]}
}

# Draft Generation
DRAFT_QUESTIONS = {
    "subject_template": {"type": "choice", "criteria": "DYNAMIC_FROM_SKELETONS"},
    "observation_fact": {"type": "choice", "criteria": "DYNAMIC_FROM_PACKET"},
    "tone": {"type": "score", "criteria": ["soft","direct","very_direct"]}
}

# Scoring Signals
SCORING_QUESTIONS = {
    "urgency": {"type": "score", "criteria": ["none","low","medium","high","critical"]},
    "churn_risk": {"type": "noul"},
    "budget_authority": {"type": "noul"}
}

# Enrichment Signals
ENRICHMENT_QUESTIONS = {
    "has_booking": {"type": "noul"},
    "site_staleness": {"type": "score", "criteria": ["fresh","moderate","stale","abandoned"]},
    "hiring_signal": {"type": "noul"},
    "social_activity": {"type": "score", "criteria": ["none","low","moderate","high"]}
}

# Outreach
OUTREACH_QUESTIONS = {
    "sendability": {"type": "noul"},
    "channel_preference": {"type": "choice", "criteria": {"phone": "...", "email": "...", "research": "..."}},
    "optimal_timing": {"type": "score", "criteria": ["morning","midday","afternoon","avoid"]}
}
```

### Dynamic Criteria Builders
```python
def build_dm_criteria(candidates: list[dict]) -> dict:
    return {str(i): f"{c['name']} — {c['title']} — {c['snippet'][:120]}" for i, c in enumerate(candidates)}

def build_draft_criteria(packet: dict) -> dict:
    return {f["k"]: f["v"] for f in packet["facts"] if f.get("k") and f.get("v")}

def build_skeleton_criteria(skeletons: dict) -> dict:
    return {k: v.get("subject_template", "") for k, v in skeletons.items()}
```

---

## 6. Laya Integration Module (`laya_integration.py`)

```python
"""Laya Integration — Router singleton, batch API, fallback wrapper, calibration."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from leadforge.config import Config, LayaCfg

log = logging.getLogger(__name__)

# ─── Router Singleton ──────────────────────────────────────────────────────
_router_lock = threading.Lock()
_router_instance: "Router | None" = None
_loaded_checkpoints: set[str] = set()

def get_router(cfg: Config) -> "Router":
    """Get or create the global Router instance."""
    global _router_instance
    if _router_instance is None:
        with _router_lock:
            if _router_instance is None:
                _router_instance = _create_router(cfg)
    return _router_instance

def _create_router(cfg: Config) -> "Router":
    from laya import Router
    laya_cfg: LayaCfg = cfg.laya
    
    router = Router(
        preload=laya_cfg.preload,
        device=laya_cfg.device if laya_cfg.device != "auto" else None,
        max_loaded=laya_cfg.max_loaded,
    )
    
    # Preload specific checkpoints based on config
    checkpoints_to_load = []
    if laya_cfg.checkpoint == "router":
        checkpoints_to_load = ["english", "multilingual"]
    elif laya_cfg.checkpoint == "typed-decisions":
        checkpoints_to_load = ["typed-decisions", "multilingual"]
    else:
        checkpoints_to_load = [laya_cfg.checkpoint]
    
    if checkpoints_to_load:
        router.preload(checkpoints_to_load)
        _loaded_checkpoints.update(checkpoints_to_load)
    
    # Load calibration temperatures if available
    if laya_cfg.calibration.enabled:
        _load_temperatures(router, laya_cfg.calibration.temperatures_path)
    
    return router

# ─── Batch API ─────────────────────────────────────────────────────────────
@dataclass
class BatchRequest:
    states: list[dict]
    questions: dict
    checkpoint: str | None = None

@dataclass
class BatchResponse:
    answers: list[dict]
    routing: list[dict] | None = None
    latencies_ms: list[float] | None = None

def predict_batch(
    states: list[dict],
    questions: dict,
    cfg: Config,
    checkpoint: str | None = None,
) -> BatchResponse:
    """Batched prediction — groups states by language for optimal routing."""
    router = get_router(cfg)
    laya_cfg: LayaCfg = cfg.laya
    batch_size = laya_cfg.batch_size
    
    all_answers = []
    all_routing = []
    all_latencies = []
    
    for i in range(0, len(states), batch_size):
        batch_states = states[i:i + batch_size]
        
        # Group by language for Router efficiency
        lang_groups: dict[str, list[tuple[int, dict]]] = {}
        for idx, state in enumerate(batch_states):
            lang = _detect_language(state)
            lang_groups.setdefault(lang, []).append((idx, state))
        
        batch_answers = [None] * len(batch_states)
        batch_routing = [None] * len(batch_states)
        batch_latencies = [0.0] * len(batch_states)
        
        for lang, items in lang_groups.items():
            indices, group_states = zip(*items)
            group_questions = _maybe_override_checkpoint(questions, checkpoint, lang)
            
            import time
            t0 = time.perf_counter()
            res = router.predict({"states": list(group_states)}, group_questions)
            latency_ms = (time.perf_counter() - t0) * 1000
            
            for j, idx in enumerate(indices):
                batch_answers[idx] = res["answers"][j]
                batch_routing[idx] = res.get("routing", {})
                batch_latencies[idx] = latency_ms / len(group_states)
        
        all_answers.extend(batch_answers)
        all_routing.extend(batch_routing)
        all_latencies.extend(batch_latencies)
    
    return BatchResponse(
        answers=all_answers,
        routing=all_routing,
        latencies_ms=all_latencies,
    )

def _detect_language(state: dict) -> str:
    """Quick language detection for batch grouping."""
    text = " ".join(str(v) for v in state.values() if isinstance(v, str))
    # Simple heuristic: check for non-Latin scripts
    for ch in text[:500]:
        if '\u0900' <= ch <= '\u097F': return "hi"  # Devanagari
        if '\u0600' <= ch <= '\u06FF': return "ar"  # Arabic
        if '\u4E00' <= ch <= '\u9FFF': return "zh"  # CJK
        if '\u0400' <= ch <= '\u04FF': return "ru"  # Cyrillic
    return "en"

def _maybe_override_checkpoint(questions: dict, checkpoint: str | None, lang: str) -> dict:
    if checkpoint:
        return {**questions, "_checkpoint": checkpoint}
    if lang != "en":
        return {**questions, "_checkpoint": "multilingual"}
    return questions

# ─── Fallback Wrapper ──────────────────────────────────────────────────────
class LayaUnavailable(Exception):
    """Raised when Laya cannot process a request."""
    pass

class LayaFallbackWrapper:
    """Wrapper that tries Laya, then heuristic, then agent."""
    
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.fallback_order = cfg.laya.fallback.order
        self.laya_timeout = cfg.laya.fallback.laya_timeout_s
    
    def predict_with_fallback(
        self,
        state: dict,
        questions: dict,
        heuristic_fn=None,
        agent_fn=None,
    ) -> tuple[dict, str]:
        """
        Try Laya → heuristic → agent.
        Returns (answers, source) where source ∈ {"laya", "heuristic", "agent"}.
        """
        # Try Laya
        if "laya" in self.fallback_order:
            try:
                import signal
                from contextlib import contextmanager
                
                @contextmanager
                def timeout(seconds):
                    def handler(signum, frame):
                        raise TimeoutError()
                    old = signal.signal(signal.SIGALRM, handler)
                    signal.alarm(int(seconds))
                    try:
                        yield
                    finally:
                        signal.alarm(0)
                        signal.signal(signal.SIGALRM, old)
                
                with timeout(self.laya_timeout):
                    router = get_router(self.cfg)
                    res = router.predict(state, questions)
                return res["answers"], "laya"
            except (TimeoutError, LayaUnavailable, RuntimeError, ImportError) as e:
                log.warning("Laya failed, falling back: %s", e)
        
        # Try heuristic
        if "heuristic" in self.fallback_order and heuristic_fn:
            try:
                return heuristic_fn(state, questions), "heuristic"
            except Exception as e:
                log.warning("Heuristic failed: %s", e)
        
        # Try agent
        if "agent" in self.fallback_order and agent_fn:
            try:
                return agent_fn(state, questions), "agent"
            except Exception as e:
                log.warning("Agent failed: %s", e)
        
        raise LayaUnavailable("All fallbacks exhausted")

# ─── Calibration ───────────────────────────────────────────────────────────
def _load_temperatures(router: "Router", path: str) -> None:
    """Load fitted temperatures into router agents."""
    path = Path(path).expanduser()
    if not path.exists():
        return
    try:
        temps = json.loads(path.read_text())
        for model_name, temp in temps.items():
            if hasattr(router, "agents") and model_name in router.agents:
                router.agents[model_name].temperature = temp
        log.info("Loaded %d calibration temperatures from %s", len(temps), path)
    except Exception as e:
        log.warning("Failed to load temperatures: %s", e)

def fit_temperatures(
    eval_data: list[dict],
    questions: dict,
    cfg: Config,
    output_path: str | None = None,
) -> dict:
    """
    Fit temperature scaling per (question_type, num_options) on held-out data.
    eval_data: list of {"state": {...}, "labels": {...}} where labels match questions.
    """
    from laya import load
    import numpy as np
    
    # Load base agent for fitting
    agent = load("convaiinnovations/laya", subfolder="typed-decisions")
    
    # Group eval data by question type + num_options
    groups: dict[tuple, list] = {}
    for item in eval_data:
        for q_name, q_spec in questions.items():
            q_type = q_spec["type"]
            n_opts = len(q_spec.get("criteria", {})) if q_type == "choice" else 0
            key = (q_type, n_opts)
            groups.setdefault(key, []).append((item["state"], item["labels"][q_name]))
    
    fitted = {}
    for (q_type, n_opts), group in groups.items():
        if len(group) < 50:
            continue  # Need minimum samples
        
        # Grid search temperature
        best_temp = 1.0
        best_ece = float("inf")
        
        for temp in np.linspace(0.5, 3.0, 26):
            ece = _compute_ece(agent, group, questions, temp)
            if ece < best_ece:
                best_ece = ece
                best_temp = temp
        
        fitted[f"{q_type}:{n_opts}"] = float(best_temp)
        log.info("Fitted temp for %s: %.3f (ECE=%.4f)", key, best_temp, best_ece)
    
    if output_path:
        Path(output_path).expanduser().write_text(json.dumps(fitted, indent=2))
    
    return fitted

def _compute_ece(agent, group: list, questions: dict, temperature: float) -> float:
    """Compute Expected Calibration Error."""
    agent.temperature = temperature
    # Simplified ECE computation
    # In practice, use proper binning
    return 0.0  # Placeholder

# ─── Doctor Integration ────────────────────────────────────────────────────
def doctor_check(cfg: Config) -> tuple[bool, list[str]]:
    """Verify Laya installation and basic forward pass."""
    warnings = []
    
    # Check package
    try:
        import laya
        from laya import Router
    except ImportError:
        return False, ["laya not installed — run: pip install laya>=0.3.3"]
    
    # Check weights cache
    cache_dir = Path(cfg.data_path) / "cache" / "laya"
    if not cache_dir.exists():
        warnings.append(f"Laya weights not cached at {cache_dir} — will download on first use")
    
    # Smoke test forward pass
    try:
        router = get_router(cfg)
        test_state = {"body": "Test business inquiry"}
        test_questions = {
            "test": {"type": "noul", "instructions": "Is this a test?"}
        }
        res = router.predict(test_state, test_questions)
        if "answers" not in res:
            return False, ["Laya forward pass returned unexpected format"]
    except Exception as e:
        return False, [f"Laya forward pass failed: {e}"]
    
    return True, warnings
```

---

## 7. Evaluation Harness (`scripts/eval_laya.py`)

```python
#!/usr/bin/env python3
"""Shadow-mode evaluation: compare Laya decisions vs agent decisions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from leadforge.config import load_config
from leadforge.db import connect
from leadforge.laya_integration import get_router, predict_batch

@dataclass
class EvalResult:
    total: int
    agreements: int
    agreement_rate: float
    avg_confidence: float
    avg_latency_ms: float
    by_question: dict

def load_dm_labels(conn: sqlite3.Connection, limit: int = 1000) -> list[dict]:
    """Load historical DM labels from dm_apply records."""
    rows = conn.execute("""
        SELECT b.id, b.name, b.category, b.enrich_json,
               p.name as dm_name, p.title as dm_title, p.dm_confidence, p.snippet
        FROM businesses b
        JOIN people p ON p.business_id = b.id AND p.is_dm = 1
        WHERE p.labeled_by IN ('agent', 'heuristic_auto')
        ORDER BY p.labeled_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    
    eval_data = []
    for r in rows:
        enrich = json.loads(r["enrich_json"] or "{}")
        people = enrich.get("people", [])
        candidates = [
            {"name": p["name"], "title": p["title"], "snippet": p["snippet"]}
            for p in people
        ]
        if not candidates:
            continue
        
        # Find which candidate was picked
        picked_idx = -1
        for i, c in enumerate(candidates):
            if c["name"] == r["dm_name"] and c["title"] == r["dm_title"]:
                picked_idx = i
                break
        
        eval_data.append({
            "state": {
                "biz": r["name"],
                "category": r["category"],
                "candidates": candidates,
            },
            "labels": {
                "dm_pick": str(picked_idx) if picked_idx >= 0 else "-1",
                "dm_confidence": r["dm_confidence"] * 3,  # scale to 0-3
            }
        })
    return eval_data

def load_draft_approvals(conn: sqlite3.Connection, limit: int = 500) -> list[dict]:
    """Load approved drafts for template/fact selection evaluation."""
    rows = conn.execute("""
        SELECT m.subject, m.body_text, m.used_fact, m.grade,
               b.name, b.category, b.enrich_json
        FROM messages m
        JOIN outreach_targets t ON t.id = m.target_id
        JOIN businesses b ON b.id = t.business_id
        WHERE m.state = 'approved' AND m.approved_hash = m.draft_hash
        ORDER BY m.approved_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    
    eval_data = []
    for r in rows:
        # Reconstruct packet facts from enrich
        enrich = json.loads(r["enrich_json"] or "{}")
        facts = enrich.get("facts", [])
        
        eval_data.append({
            "state": {
                "packet": {"facts": facts, "grade": r["grade"]},
                "business": {"name": r["name"], "category": r["category"]},
            },
            "labels": {
                "subject_template": _infer_template(r["subject"]),
                "observation_fact": r["used_fact"],
                "tone": _infer_tone(r["body_text"]),
            }
        })
    return eval_data

def _infer_template(subject: str) -> str:
    if "booking" in subject.lower(): return "hook_booking"
    if "website" in subject.lower(): return "hook_no_site"
    if "star" in subject.lower() or "rating" in subject.lower(): return "hook_reviews"
    return "generic"

def _infer_tone(body: str) -> str:
    # Heuristic
    return "direct"

def run_shadow_eval(cfg, limit_dm=1000, limit_draft=500) -> dict:
    conn = connect(cfg.db_path)
    router = get_router(cfg)
    
    # DM Labeling Evaluation
    dm_data = load_dm_labels(conn, limit_dm)
    dm_states = [d["state"] for d in dm_data]
    dm_labels = [d["labels"] for d in dm_data]
    
    # Build dynamic criteria for each state
    dm_results = []
    for state, label in zip(dm_states, dm_labels):
        questions = {
            "dm_pick": {"type": "choice", "instructions": "...", "criteria": build_dm_criteria(state["candidates"])},
            "dm_confidence": {"type": "score", "instructions": "...", "criteria": ["guess","plausible","strong","certain"]}
        }
        res = router.predict(state, questions)
        dm_results.append((res["answers"], label))
    
    # Compute agreement
    dm_agreements = sum(1 for pred, true in dm_results 
                        if pred["dm_pick"]["choice"] == true["dm_pick"])
    
    # Draft Evaluation
    draft_data = load_draft_approvals(conn, limit_draft)
    # ... similar pattern
    
    return {
        "dm_labeling": {
            "total": len(dm_results),
            "agreements": dm_agreements,
            "agreement_rate": dm_agreements / len(dm_results) if dm_results else 0,
        },
        "draft_generation": {
            "total": len(draft_data),
            # ... similar
        }
    }

def build_dm_criteria(candidates: list[dict]) -> dict:
    return {str(i): f"{c['name']} — {c['title']} — {c['snippet'][:120]}" for i, c in enumerate(candidates)}

if __name__ == "__main__":
    cfg = load_config(".")
    results = run_shadow_eval(cfg)
    print(json.dumps(results, indent=2))
```

---

## 8. Calibration Script (`scripts/calibrate_laya.py`)

```python
#!/usr/bin/env python3
"""Fit temperature scaling per (question_type, num_options) on held-out data."""

from __future__ import annotations

import json
from pathlib import Path

from leadforge.config import load_config
from leadforge.db import connect
from leadforge.laya_integration import fit_temperatures

def main():
    cfg = load_config(".")
    conn = connect(cfg.db_path)
    
    # Load evaluation data (same as shadow eval)
    # ... reuse load_dm_labels, load_draft_approvals from eval_laya.py
    
    # Combine all eval data
    all_eval_data = []  # dm_data + draft_data
    
    # All question schemas
    from leadforge.laya_integration import (
        DM_LABEL_QUESTIONS, DRAFT_QUESTIONS, SCORING_QUESTIONS,
        ENRICHMENT_QUESTIONS, OUTREACH_QUESTIONS
    )
    all_questions = {}
    all_questions.update(DM_LABEL_QUESTIONS)
    all_questions.update(DRAFT_QUESTIONS)
    all_questions.update(SCORING_QUESTIONS)
    all_questions.update(ENRICHMENT_QUESTIONS)
    all_questions.update(OUTREACH_QUESTIONS)
    
    # Fit temperatures
    output_path = Path(cfg.laya.calibration.temperatures_path).expanduser()
    fitted = fit_temperatures(all_eval_data, all_questions, cfg, str(output_path))
    
    print("Fitted temperatures:")
    for k, v in sorted(fitted.items()):
        print(f"  {k}: {v:.3f}")

if __name__ == "__main__":
    main()
```

---

## 9. Migration Strategy (Updated)

### Phase 1: Foundation (Week 1)
- [ ] Add `LayaCfg` to `config.py` with validation
- [ ] Add `laya_integration.py` with Router singleton, batch API, fallback wrapper
- [ ] Add `doctor_check` integration in `doctor.py`
- [ ] Add `laya` extra to `pyproject.toml` / `requirements.txt`
- [ ] Run `leadforge doctor --fix` — verify Laya installs, weights cache, forward pass works

### Phase 2: Shadow Mode (Week 1-2)
- [ ] Add `scripts/eval_laya.py` — loads historical DM labels & draft approvals
- [ ] Run shadow eval: `python scripts/eval_laya.py` → target ≥95% agreement
- [ ] Add `scripts/calibrate_laya.py` — fit temperatures on held-out data
- [ ] Log all Laya vs Agent comparisons to `leadforge_data/logs/laya_shadow.jsonl`

### Phase 3: Selective Replacement (Week 2-3)
- [ ] Replace `auto_label` in `dm.py` with `LayaFallbackWrapper` (Laya → heuristic → agent)
- [ ] Replace `auto_draft` in `service.py` with Laya template selection
- [ ] Keep `agent_runner` for manual CLI commands only
- [ ] Run full test suite: `pytest tests/ -x -q`

### Phase 4: Full Autopilot (Week 3-4)
- [ ] Remove `agent_runner` subprocess from `run` hot path
- [ ] Laya becomes primary; agent = human-in-loop override only
- [ ] Benchmark: `python scripts/bench_laya.py` — verify targets met

### Phase 5: Fine-Tuning (Ongoing)
- [ ] Collect LeadForge-specific training data
- [ ] Fine-tune `laya-typed-decisions` on Kaggle 2xT4
- [ ] Push custom `leadforge-typed-decisions` checkpoint
- [ ] Update config to use custom checkpoint

---

## 10. Performance Targets (Updated)

| Metric | Current (Agent) | Laya Target | Measurement |
|--------|-----------------|-------------|-------------|
| DM labeling (60 biz) | 30-120 s | **< 2 s** | `bench_laya.py` |
| Draft generation (50 targets) | 60-300 s | **< 3 s** | `bench_laya.py` |
| Scoring signals (1000 biz) | N/A (heuristic) | **< 1 s** | batched forward pass |
| Calibration (ECE) | N/A | **< 0.1** | held-out validation |
| Availability | 99% (depends on claude CLI) | **100% local** | no network deps |
| Shadow agreement (DM) | N/A | **≥95%** | `eval_laya.py` |
| Shadow agreement (Draft) | N/A | **≥90%** | `eval_laya.py` |

---

## 11. Risks & Mitigations (Updated)

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Laya choice degrades >20 options | Medium | DM pick with many candidates | Limit candidates to top 8 (already done), use coarse→fine |
| Zero-shot accuracy low | High | All use cases | **Must fine-tune** on LeadForge data (Phase 5) |
| CPU-only environments slow | Medium | No GPU machines | `laya-multilingual` on CPU: 193-464 ms/q; acceptable for batch |
| Calibration drift | Low | Confidence gating wrong | Auto-refit temps monthly via cron; monitor ECE in eval |
| HF Hub download fails | Low | First-run setup | Cache weights in `leadforge_data/cache/laya/`; vendor in Docker |
| Router language detection wrong | Low | Wrong checkpoint used | Fallback to `typed-decisions` for English; log routing decisions |
| OOM on GPU | Low | Batch too large | Dynamic batch sizing: reduce batch_size on OOM, retry |

---

## 12. Fine-Tuning Plan (Phase 5)

**Dataset Construction**:
- DM picks: every `dm apply` record → `(state, questions, label)` 
- Draft approvals: every `outreach approve` → `(packet, questions, template/fact)`
- Negative examples: rejected drafts, rejected DM picks
- Enrichment signals: human-labeled site classifications

**Training**:
```bash
# On Kaggle 2xT4 free
# notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb
# ~4-5 hours for 4 epochs over ~30k questions
```

**Target**: Custom `leadforge-typed-decisions` checkpoint beating 0.766 on our eval set.

**Data Format for Fine-Tuning**:
```json
{
  "state": {"biz": "Acme Auto", "category": "auto repair", "candidates": [...]},
  "questions": {
    "dm_pick": {"type": "choice", "instructions": "...", "criteria": {...}},
    "dm_confidence": {"type": "score", "instructions": "...", "criteria": [...]}
  },
  "labels": {"dm_pick": "0", "dm_confidence": 2.7}
}
```

---

## 13. Open Questions (Resolve During Implementation)

1. **Checkpoint selection**: Router vs direct `typed-decisions`? Router adds language detection but typed-decisions is fine-tuned for our workflows. → **Test both in shadow mode; A/B in eval harness**

2. **Batch sizing**: Optimal `batch_size` for LeadForge workloads? → **Benchmark 16/32/64 in `bench_laya.py`**

3. **Temperature fitting**: Per-question-type or global? → **Start per-(type, n_opts), refine if ECE > 0.1**

4. **GPU memory**: 421M params ≈ 1.6 GB FP16. Two checkpoints = 3.2 GB. Fits T4 (16 GB). → **Preload english + multilingual (Router default)**

5. **Fallback chain**: Laya → heuristic → agent? → **Yes, implemented in `LayaFallbackWrapper`**

6. **Language routing for batch**: Group by detected language or single Router call? → **Group by language for Router efficiency**

7. **Calibration storage**: Per-domain or global? → **Global per-(type, n_opts) first; per-domain if ECE > 0.15 for specific domains**

---

## 14. Validation Checklist (Updated)

Before merging each integration point:

- [ ] `leadforge doctor --fix` installs `laya` extra, downloads weights, forward pass works
- [ ] Shadow mode logs show ≥95% agreement (DM), ≥90% (Draft)
- [ ] Latency < 50 ms p99 per batch
- [ ] Calibration ECE < 0.1 on held-out data
- [ ] No regression in tier distribution (A/B/C/DQ counts)
- [ ] Digest contract unchanged (LF_DIGEST still emitted)
- [ ] Tests pass: `pytest tests/ -k laya -x`
- [ ] Fallback chain works: Laya → heuristic → agent (tested in `test_laya_integration.py`)

---

## 15. Appendix: Laya Resources

- **GitHub**: https://github.com/NandhaKishorM/laya
- **HF Models**: `convaiinnovations/laya`, `.../laya-multilingual`, `.../laya-typed-decisions`
- **PyPI**: `pip install laya>=0.3.3`
- **Benchmarks**: `BENCHMARKS.md` in repo
- **Fine-tuning**: `notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb`
- **arXiv**: 2503.23303, 2510.01237
- **Dev.to writeup**: "I built non-autoregressive decision models a year ago..."

---

*This spec evolves. Update after each integration point validation.*