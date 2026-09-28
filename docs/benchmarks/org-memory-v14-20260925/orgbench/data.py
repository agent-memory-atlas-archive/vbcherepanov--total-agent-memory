"""Deterministic synthetic company for the organisational-memory benchmark.

Everything is derived from one integer seed. No network, no LLM.
"""
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEPARTMENTS = ("engineering", "sales", "hr", "finance")
LOAD_ONLY_DEPARTMENTS = ("legal", "ops")  # used only by E4 to reach 8 scopes
FACTS_PER_TEMPLATE = 5
UPDATED_FACTS_PER_DEPARTMENT = 10
QUESTIONS_PER_FACT = 2
SHARED_FACTS = 20
PERSONAL_NOTES_PER_USER = 3
TWIN_NOTES = 6
TWIN_PAIRS = (("engineering", "sales"), ("hr", "finance"), ("sales", "hr"),
              ("finance", "engineering"), ("engineering", "hr"), ("sales", "finance"))

FIRST = ("Anna", "Boris", "Chen", "Dana", "Emil", "Farah", "Gleb", "Hana", "Ivan", "Jonas", "Kira", "Leo",
         "Maya", "Nika", "Oleg", "Pia", "Quinn", "Rosa", "Sven", "Tara", "Uma", "Viktor", "Wren", "Yara", "Zoe")
LAST = ("Adler", "Brandt", "Costa", "Dietz", "Eriksen", "Fischer", "Garcia", "Holm", "Ibarra", "Jensen",
        "Kowalski", "Lind", "Moreau", "Novak", "Okafor", "Petrov", "Quist", "Rossi", "Sato", "Tanaka")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
QUARTERS = ("Q1 2026", "Q2 2026", "Q3 2026", "Q4 2025")


def person(rng):
    return f"{rng.choice(FIRST)} {rng.choice(LAST)}"


def email(name, dept):
    return name.lower().replace(" ", ".") + f"@{dept}.example.org"


def subjects(rng, left, right, count):
    pool = [f"{a} {b}" for a in left for b in right]
    rng.shuffle(pool)
    return pool[:count]


SUBJECT_WORDS = {
    "engineering": (("billing", "search", "ledger", "identity", "catalog", "routing", "metrics", "export",
                     "invoice", "payments", "gateway", "scheduler", "notification", "inventory"),
                    ("service", "indexer", "worker", "API", "pipeline", "cluster", "cache")),
    "sales": (("Northwind", "Bluefin", "Crescent", "Granite", "Harbor", "Juniper", "Keystone", "Lumen",
               "Meridian", "Orchard", "Pinnacle", "Quarry", "Redwood", "Summit"),
              ("Logistics", "Retail", "Health", "Foods", "Energy", "Media", "Freight")),
    "hr": (("Backend", "Frontend", "Data", "Support", "Sales", "Finance", "Security", "Design",
            "Recruiting", "Payroll", "Platform", "Mobile", "QA", "Legal"),
           ("Engineer", "Analyst", "Manager", "Specialist", "Lead", "Coordinator", "Associate")),
    "finance": (("Travel", "Cloud", "Hardware", "Marketing", "Training", "Office", "Software",
                 "Consulting", "Hosting", "Events", "Legal", "Insurance", "Catering", "Recruiting"),
                ("budget", "cost center", "vendor account", "reserve", "accrual", "ledger line", "fund")),
    "legal": (("NDA", "MSA", "DPA", "SOW", "licence", "lease", "policy", "warranty", "escrow", "patent",
               "trademark", "privacy", "export", "audit"),
              ("template", "review", "register", "clause", "renewal", "filing", "archive")),
    "ops": (("Berlin", "Lisbon", "Austin", "Warsaw", "Tbilisi", "Porto", "Dublin", "Riga", "Prague",
             "Oslo", "Vienna", "Madrid", "Zurich", "Tallinn"),
            ("office", "warehouse", "datacenter", "parking", "reception", "mailroom", "lab")),
}

# Each template: category, fact, direct question, paraphrased question, value generators, updatable slot.
TEMPLATES = {
    "engineering": [
        ("rule", "Deployments of the {s} are frozen every {day} after {hour}:00 UTC; exceptions need approval from the on-call lead.",
         "When are deployments of the {s} frozen?", "Can I ship a change to the {s} late in the week?",
         {"day": WEEKDAYS, "hour": range(12, 20)}, "hour"),
        ("rule", "Every pull request touching the {s} needs {n} approvals and a green CI pipeline before merge.",
         "How many approvals does a pull request for the {s} need?", "What must happen before my {s} change can be merged?",
         {"n": range(2, 5)}, "n"),
        ("decision", "We chose {db} as the primary datastore for the {s} because of {why}.",
         "Which database does the {s} use?", "What storage engine did the team pick for the {s}, and why?",
         {"db": ("PostgreSQL", "CockroachDB", "ScyllaDB", "SQLite", "ClickHouse"),
          "why": ("strict transactions", "cheap horizontal scaling", "low read latency", "simple operations")}, "db"),
        ("decision", "The {s} public interface moved from {old} to {new} in {q}.",
         "What protocol does the {s} expose now?", "Did the {s} migrate its interface recently, and to what?",
         {"old": ("REST", "SOAP", "Thrift"), "new": ("gRPC", "GraphQL", "JSON-RPC"), "q": QUARTERS}, "new"),
        ("procedure", "To rotate secrets for the {s}, run make rotate-{slug} and restart its pods within {n} minutes.",
         "How do I rotate secrets for the {s}?", "What is the credential refresh routine for the {s}?",
         {"n": range(5, 31)}, "n"),
        ("procedure", "A severity {sev} incident on the {s} pages the {rota} rotation and must be acknowledged within {n} minutes.",
         "Who gets paged for a severity {sev} incident on the {s}?", "How fast must an outage of the {s} be acknowledged?",
         {"sev": ("1", "2"), "rota": ("platform", "backend", "data", "SRE"), "n": range(5, 16)}, "n"),
        ("contact", "The owner of the {s} is {p} ({mail}); the backup owner is {p2}.",
         "Who owns the {s}?", "Whom should I contact about problems with the {s}?",
         {"p": "person", "p2": "person"}, "p"),
        ("contact", "Architecture questions about the {s} go to {p} in the #arch-{slug} channel.",
         "Who answers architecture questions about the {s}?", "Where do I ask design questions for the {s}?",
         {"p": "person"}, "p"),
        ("confidential", "Restricted: the production vault path for the {s} is secret/prod/{slug}/{n} and the break-glass key id is BG-{k}.",
         "What is the production vault path for the {s}?", "Where are the production secrets of the {s} kept?",
         {"n": range(100, 999), "k": range(1000, 9999)}, "n"),
        ("confidential", "Restricted: the {s} has an unpatched vulnerability tracked as SEC-{k}, fix due {day}.",
         "Is there an open vulnerability in the {s}?", "Which security ticket is pending for the {s}?",
         {"k": range(1000, 9999), "day": WEEKDAYS}, "day"),
    ],
    "sales": [
        ("rule", "Discounts above {n} percent for {s} require approval from the regional sales director.",
         "What discount for {s} needs director approval?", "How large a price cut can I give {s} on my own?",
         {"n": range(10, 31)}, "n"),
        ("rule", "Quotes for {s} are valid for {n} days and must use the {cur} price list.",
         "How long is a quote for {s} valid?", "Which price list and validity apply to offers for {s}?",
         {"n": (14, 30, 45, 60, 90), "cur": ("EUR", "USD", "GBP")}, "n"),
        ("decision", "We decided to sell {s} the {tier} plan on a {term}-year term starting {q}.",
         "Which plan did we sell to {s}?", "What subscription tier and term does {s} have?",
         {"tier": ("Starter", "Growth", "Enterprise", "Premium"), "term": range(1, 4), "q": QUARTERS}, "tier"),
        ("decision", "The {s} renewal will be handled by the {team} team from {q}.",
         "Which team handles the {s} renewal?", "Who takes over the renewal of {s}?",
         {"team": ("enterprise", "mid-market", "partner", "inside sales"), "q": QUARTERS}, "team"),
        ("procedure", "To onboard {s}, send the welcome pack, book a kickoff within {n} business days and open a CRM case.",
         "How do we onboard {s}?", "What are the first steps after {s} signs?",
         {"n": range(2, 11)}, "n"),
        ("procedure", "Escalations from {s} go first to the account manager, then to the {team} lead after {n} hours.",
         "How are escalations from {s} handled?", "What is the escalation path when {s} complains?",
         {"team": ("support", "success", "solutions"), "n": range(4, 49)}, "n"),
        ("contact", "The account manager for {s} is {p} ({mail}); their buyer is {p2}.",
         "Who is the account manager for {s}?", "Whom do I talk to about the {s} account?",
         {"p": "person", "p2": "person"}, "p"),
        ("contact", "Legal redlines for {s} are coordinated by {p} on the deal desk.",
         "Who coordinates legal redlines for {s}?", "Who on the deal desk handles contract edits for {s}?",
         {"p": "person"}, "p"),
        ("confidential", "Confidential: {s} pays {n} thousand USD per year with a {d} percent discount.",
         "How much does {s} pay per year?", "What are the commercial terms of the {s} deal?",
         {"n": range(40, 900), "d": range(5, 35)}, "n"),
        ("confidential", "Confidential: {s} has an exit clause after {n} months if uptime falls below {u} percent.",
         "Does {s} have an exit clause?", "Under which conditions can {s} leave the contract early?",
         {"n": range(3, 25), "u": ("99.0", "99.5", "99.9")}, "n"),
    ],
    "hr": [
        ("rule", "{s} candidates must complete {n} interview rounds including one take-home task.",
         "How many interview rounds does a {s} candidate have?", "What is the hiring loop for a {s}?",
         {"n": range(3, 7)}, "n"),
        ("rule", "A {s} may work remotely up to {n} days per week after the probation period.",
         "How many remote days can a {s} take?", "What is the home-office allowance for a {s}?",
         {"n": range(1, 6)}, "n"),
        ("decision", "The {s} role moves to the {grade} grade family from {q}.",
         "Which grade family does the {s} role use?", "Was the {s} position re-graded?",
         {"grade": ("IC2", "IC3", "IC4", "M1", "M2"), "q": QUARTERS}, "grade"),
        ("decision", "We decided that {s} hires get a {n} month probation period.",
         "How long is probation for a {s}?", "What trial period applies to new {s} staff?",
         {"n": range(2, 7)}, "n"),
        ("procedure", "Onboarding a {s} takes {n} working days: laptop, accounts, buddy assignment and policy training.",
         "How long does onboarding a {s} take?", "What happens in the first days of a new {s}?",
         {"n": range(2, 11)}, "n"),
        ("procedure", "To offboard a {s}, collect equipment, revoke access within {n} hours and run an exit interview.",
         "How do we offboard a {s}?", "What are the steps when a {s} leaves?",
         {"n": range(1, 25)}, "n"),
        ("contact", "The HR business partner for {s} staff is {p} ({mail}).",
         "Who is the HR partner for {s} staff?", "Whom should a {s} contact about HR matters?",
         {"p": "person"}, "p"),
        ("contact", "Recruiting for {s} openings is run by {p}; the hiring manager is {p2}.",
         "Who recruits for {s} openings?", "Who runs hiring for a {s} vacancy?",
         {"p": "person", "p2": "person"}, "p"),
        ("confidential", "Confidential: the salary band for a {s} is {lo} to {hi} thousand EUR.",
         "What is the salary band for a {s}?", "How much does a {s} earn?",
         {"lo": range(40, 90), "hi": range(90, 160)}, "hi"),
        ("confidential", "Confidential: the {s} retention bonus pool is {n} thousand EUR for {q}.",
         "How big is the {s} retention bonus pool?", "What retention money is set aside for {s} staff?",
         {"n": range(10, 300), "q": QUARTERS}, "n"),
    ],
    "finance": [
        ("rule", "Spending from the {s} above {n} thousand EUR needs CFO sign-off.",
         "When does spending from the {s} need CFO sign-off?", "What is the approval limit on the {s}?",
         {"n": range(5, 100)}, "n"),
        ("rule", "Invoices charged to the {s} are paid on net {n} terms.",
         "What payment terms apply to the {s}?", "How quickly are invoices on the {s} paid?",
         {"n": (15, 30, 45, 60)}, "n"),
        ("decision", "The {s} is capped at {n} thousand EUR for {q}.",
         "What is the cap on the {s}?", "How much money is in the {s} this quarter?",
         {"n": range(20, 800), "q": QUARTERS}, "n"),
        ("decision", "We moved the {s} to the {bank} account from {q}.",
         "Which bank account holds the {s}?", "Where is the {s} banked now?",
         {"bank": ("Deutsche", "ING", "Revolut Business", "HSBC", "Santander"), "q": QUARTERS}, "bank"),
        ("procedure", "To reimburse from the {s}, submit receipts in the expense tool within {n} days of purchase.",
         "How do I get reimbursed from the {s}?", "What is the expense claim process for the {s}?",
         {"n": range(7, 61)}, "n"),
        ("procedure", "Month-end close for the {s} runs on business day {n} and needs a variance note.",
         "When is month-end close for the {s}?", "Which day is the {s} closed each month?",
         {"n": range(2, 8)}, "n"),
        ("contact", "The controller for the {s} is {p} ({mail}).",
         "Who is the controller for the {s}?", "Whom do I ask about entries on the {s}?",
         {"p": "person"}, "p"),
        ("contact", "Purchase orders on the {s} are approved by {p}; backup approver is {p2}.",
         "Who approves purchase orders on the {s}?", "Who signs off POs for the {s}?",
         {"p": "person", "p2": "person"}, "p"),
        ("confidential", "Restricted: the {s} IBAN ends in {n} and the signatory PIN reference is PIN-{k}.",
         "What are the last digits of the {s} IBAN?", "Which bank details belong to the {s}?",
         {"n": range(1000, 9999), "k": range(100, 999)}, "n"),
        ("confidential", "Restricted: an audit finding on the {s} of {n} thousand EUR is still open.",
         "Is there an open audit finding on the {s}?", "How large is the unresolved audit issue on the {s}?",
         {"n": range(5, 400)}, "n"),
    ],
}
# legal/ops (E4 load only) reuse finance/engineering template shapes with their own subjects.
TEMPLATES["legal"] = TEMPLATES["finance"]
TEMPLATES["ops"] = TEMPLATES["engineering"]

SHARED_TEMPLATES = (
    "Company-wide: the {s} is open {a}:00 to {b}:00 on weekdays.",
    "Company-wide: the all-hands meeting about {s} takes place every {day}.",
    "Company-wide: questions about the {s} go to the service desk queue {k}.",
    "Company-wide: the {s} policy was updated in {q}.",
)
SHARED_SUBJECTS = ("cafeteria", "library", "gym", "front desk", "mail room", "bike storage", "parent room",
                   "IT help bar", "quiet room", "print shop", "travel desk", "security desk", "rooftop terrace",
                   "wellness room", "game room", "badge office", "coat room", "shuttle stop", "garden", "archive room")

TWIN_TEXTS = (
    "Twin note {i}: the quarterly offsite planning review happens in meeting room {room}.",
)


@dataclass
class Fact:
    key: str
    department: str
    category: str
    canary: str
    subject: str
    content: str
    content_v2: str | None = None
    questions: list[dict] = field(default_factory=list)


def _values(rng, spec):
    out = {}
    for name, source in spec.items():
        if source == "person":
            out[name] = person(rng)
        else:
            out[name] = rng.choice(list(source))
    return out


def _render(template, subject, values, dept):
    slug = subject.lower().replace(" ", "-")
    extra = {"s": subject, "slug": slug}
    if "p" in values:
        extra["mail"] = email(values["p"], dept)
    return template.format(**values, **extra)


def _new_value(rng, spec, old):
    if spec == "person":
        choices = [f"{a} {b}" for a in FIRST for b in LAST]
    else:
        choices = list(spec)
    choices = [c for c in choices if c != old]
    return rng.choice(choices)


def department_facts(rng, dept, canaries):
    left, right = SUBJECT_WORDS[dept]
    count = len(TEMPLATES[dept]) * FACTS_PER_TEMPLATE
    pool = subjects(rng, left, right, count)
    facts = []
    index = 0
    for t_index, (category, fact_t, q1, q2, spec, upd) in enumerate(TEMPLATES[dept]):
        for _ in range(FACTS_PER_TEMPLATE):
            subject = pool[index]
            index += 1
            values = _values(rng, spec)
            canary = canaries.pop()
            body = _render(fact_t, subject, values, dept)
            fact = Fact(key=f"{dept}-{index:03d}", department=dept, category=category,
                        canary=f"CANARY-{dept}-{canary:04d}", subject=subject,
                        content=f"{body} Ref {f'CANARY-{dept}-{canary:04d}'}.")
            fact._values, fact._template, fact._spec, fact._upd = values, fact_t, spec, upd  # type: ignore[attr-defined]
            for kind, qt in (("direct", q1), ("paraphrase", q2)):
                fact.questions.append({"qid": f"{fact.key}-{kind}", "kind": kind,
                                       "question": _render(qt, subject, values, dept)})
            facts.append(fact)
    updated = rng.sample(range(len(facts)), UPDATED_FACTS_PER_DEPARTMENT)
    for i in sorted(updated):
        fact = facts[i]
        values = dict(fact._values)  # type: ignore[attr-defined]
        values[fact._upd] = _new_value(rng, fact._spec[fact._upd], values[fact._upd])  # type: ignore[attr-defined]
        body = _render(fact._template, fact.subject, values, dept)  # type: ignore[attr-defined]
        fact.content_v2 = f"{body} Ref {fact.canary}."
    return facts


def build(seed: int) -> dict:
    rng = random.Random(seed)
    canaries = {d: rng.sample(range(1000, 10000), 200) for d in (*DEPARTMENTS, *LOAD_ONLY_DEPARTMENTS, "shared")}
    departments = {}
    for dept in (*DEPARTMENTS, *LOAD_ONLY_DEPARTMENTS):
        facts = department_facts(rng, dept, canaries[dept])
        departments[dept] = [{k: v for k, v in asdict(f).items()} for f in facts]
    shared = []
    for i, subject in enumerate(SHARED_SUBJECTS[:SHARED_FACTS]):
        template = SHARED_TEMPLATES[i % len(SHARED_TEMPLATES)]
        body = template.format(s=subject, a=rng.randint(7, 10), b=rng.randint(17, 22), day=rng.choice(WEEKDAYS),
                               k=f"SD-{rng.randint(100, 999)}", q=rng.choice(QUARTERS))
        canary = f"CANARY-shared-{canaries['shared'].pop():04d}"
        shared.append({"key": f"shared-{i + 1:03d}", "canary": canary, "content": f"{body} Ref {canary}."})
    users = []
    for dept in DEPARTMENTS:
        for suffix, role in (("editor1", "editor"), ("editor2", "editor"), ("reader", "reader")):
            users.append({"id": f"{dept}-{suffix}", "name": f"{dept.title()} {suffix}", "team": dept, "role": role})
    personal = {}
    for user in users:
        notes = []
        for n in range(PERSONAL_NOTES_PER_USER):
            canary = f"CANARY-personal-{user['id']}-{n + 1:02d}"
            notes.append({"canary": canary, "content": f"Private note of {user['id']}: my {rng.choice(('review', 'reminder', 'draft'))} "
                                                       f"about {rng.choice(SHARED_SUBJECTS)} is due {rng.choice(WEEKDAYS)}. Ref {canary}."})
        personal[user["id"]] = notes
    twins = []
    for i, pair in enumerate(TWIN_PAIRS[:TWIN_NOTES]):
        twins.append({"key": f"twin-{i + 1}", "departments": list(pair),
                      "content": TWIN_TEXTS[0].format(i=i + 1, room=f"{rng.choice('ABCDEF')}{rng.randint(100, 499)}")})
    return {"seed": seed, "departments": departments, "shared": shared, "users": users,
            "personal": personal, "twins": twins}


def write(seed: int, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = build(seed)
    path = out_dir / f"company-seed{seed}.json"
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    with (out_dir / f"questions-seed{seed}.jsonl").open("w") as handle:
        for dept in DEPARTMENTS:
            for fact in data["departments"][dept]:
                for q in fact["questions"]:
                    handle.write(json.dumps({**q, "department": dept, "fact": fact["key"],
                                             "updated": fact["content_v2"] is not None}) + "\n")
    return path
