# KERN Intelligence State

## Purpose
This file captures where KERN's intelligence stands now, where it is weak, and what the intended target is.

It is not product marketing.
It is a current-state intelligence note for future implementation, review, or AI-assisted planning.

## Current Intelligence Level
KERN is no longer just a chat wrapper around an LLM.

Current practical shape:
- deterministic preparation-first for many worker-facing tasks
- deterministic document-intelligence routing for many freeform local file/PDF requests
- deterministic freeform intent routing ahead of prepared-work, document, and generic chat fallback
- grounded thread-context packets for prior email/thread questions
- grounded person-context packets for customer/contact questions
- clarification-first behavior when local person/thread targets are ambiguous
- lightweight context linking across mailbox, drafts, contacts, and packets
- separate interaction-outcome recording for packet usage without silently mutating shared truth
- workflow-aware prepared work packets
- document answer packets with task intent, query plan, citations, blockers, and readiness
- stricter document disambiguation so multiple plausible matches stay visible instead of being silently collapsed
- stronger compare and summary readiness checks that require broader grounded support before KERN says a packet is ready
- readiness, blockers, and missing-input tracking
- evidence bundles with claims, negative evidence, and coverage metadata
- worker-amplifier posture instead of manager/operator posture
- bounded LLM role for wording, fallback chat, summarization, and packet-backed rewriting

Plain-English analogy:
- older KERN was like a smart junior assistant who could talk well and remember some things
- current KERN is more like an adult support worker who prepares either a work packet or a document case file before speaking

Current rough intelligence level:
- stronger than a generic assistant
- still below a real expert operator or analyst
- reliable in structured preparation paths
- much stronger in messy local freeform questions than before
- still weaker in deep business reasoning and long-range judgment

## What KERN Can Do Well Right Now
- prepare worker-facing recommendation and preparation packets from local state
- classify many local document requests deterministically before broad chat fallback
- classify many messy freeform requests into document, thread, person, workspace, or preparation routes before broad chat fallback
- build document answer packets for citation, summary, important-section, compare, and grounded Q&A flows
- build thread context packets for questions like "what did we tell this client last time"
- build person context packets for questions like "what matters for this supplier right now"
- bias toward recent or explicitly matched local documents when the user says "this PDF" or similar
- avoid hijacking generic summary requests into document mode when the request does not really point at a document
- avoid weak guessing when multiple local people or threads plausibly match; ask for clarification instead
- expose document citations, missing evidence, and clarification needs instead of guessing
- keep document comparisons blocked until both sides have real grounded support, not just title overlap
- rank work using workflow state, obligations, evidence, drafts, memory, and document context
- expose blockers instead of hiding them
- expose missing inputs instead of pretending work is ready
- keep compliance, review, document, scheduler, and correspondence workflows more grounded than before
- let the LLM rewrite from a KERN-prepared work packet or document answer packet instead of inventing from scratch
- support a worker-amplifier UX instead of a controlling manager UX

## What We Want
Core target:
- KERN is a local reasoning system with memory, retrieval, rules, readiness logic, and workflow awareness
- the LLM is the best interface layer for expression, rewriting, summarization, and clarification
- KERN should feel smart even with a weak model
- KERN should help workers do better work, not boss them around

Desired end-state:
- deterministic truth and evidence do most of the thinking
- the LLM benefits from KERN's structure instead of guessing around weak structure
- prepared work, freeform understanding, documents, and cross-workflow reasoning all become stronger
- the system grows into real symbiosis: KERN grounds, the LLM expresses

## Current Weaknesses
1. Freeform task understanding is now stronger across document, thread, and person questions, but it is still not close to full human-like natural task understanding.
2. PDF/document-specific intent detection now exists as a first-class reasoning path, but it is still simpler than full human-like task understanding.
3. Workflow state is still partly projection-on-read, not fully rebuilt from append-only domain truth.
4. Claim generation is stronger for document packets, but overall it is still template-driven and not a deep claim-by-claim proof engine.
5. Evidence coverage is better now, but still not full "proof of every statement" grounding.
6. Negative evidence handling is stronger for document, thread, and person packets, but still not comprehensive across all task types.
7. Retrieval orchestration is improved and now task-aware for document flows, but it is still not a rich general evidence planner across every domain.
8. Same-contact / same-customer / same-thread modeling now exists as a first-class layer, but it is still lightweight and shallow.
9. Customer/person modeling is still weak; KERN can resolve and summarize people better now, but it still does not deeply "know the people."
10. Long-range dependency reasoning is weak; it does not strongly model chain reactions across tasks.
11. Business judgment is still limited; it ranks and prepares better than it reasons strategically.
12. Prioritization is still assistive but not deeply outcome-aware.
13. Exception handling is still much weaker than normal-path handling.
14. Learning from behavior now records interaction outcomes more cleanly, but it is still simple and not a mature adaptive system.
15. Shadow ranking exists, but there is no full evaluation loop deciding what ranking changes truly help.
16. General chat fallback is still more LLM-led than KERN-led when no deterministic freeform, work, thread, person, or document route is available.
17. The LLM rewrite path now exists for prepared-work, document, thread, and person packets, but symbiosis is still weaker in broad non-local freeform work.
18. Knowledge-graph / archive expansion is still lighter than a real multi-hop reasoning layer.
19. Readiness/blocker logic is stronger, but still not a full prerequisite/dependency engine for every workflow.
20. Cross-domain reasoning is still weak; KERN is better inside packet/workflow slices than across multiple slices at once.

## What These Weaknesses Mean In Practice
- KERN is strongest when the task looks like a known preparation pattern or a local document/thread/person reasoning pattern.
- KERN is weaker when the user asks a broad freeform question that spans multiple local domains at once or has weak local anchors.
- KERN can now prepare evidence-backed document answers and relationship-aware local context packets, but it still does not fully reason like a domain expert.
- KERN is grounded better than before, but not yet proof-complete.
- KERN is less likely than before to fake certainty on ambiguous person/thread/document requests, but it still uses heuristic evidence planning rather than deep proof logic.
- KERN and the LLM work together better now across work packets, document packets, thread packets, and person packets, but the best symbiosis is still limited to grounded local paths.

## Short Honest Summary
Current KERN is:
- a serious worker-amplifier system
- no longer just an LLM shell
- materially stronger than before Phase 10
- visibly better at messy local freeform questions after Phase 11
- still missing several deep intelligence layers needed for truly high-end reasoning

The main future path is:
- deepen deterministic intelligence
- improve freeform understanding outside document/work packet routes
- improve proof/evidence quality
- improve readiness and dependency reasoning
- improve KERN+LLM symbiosis until the model becomes an amplifier of KERN rather than the place where missing intelligence is patched over
