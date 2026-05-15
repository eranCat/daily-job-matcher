"""Tests for scripts/scorer.py — unit tests (no API key) and integration tests (skipped without key)."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import scorer  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_job(
    role, description="", source="greenhouse", company="Acme", location="Tel Aviv"
):
    return {
        "role": role,
        "company": company,
        "location": location,
        "link": f"https://example.com/{role.replace(' ', '-')}",
        "source": source,
        "posted": None,
        "description": description,
        "match_score": None,
        "reason": None,
    }


SETTINGS = {
    "skills": [
        "React",
        "TypeScript",
        "Python",
        "Node.js",
        "Docker",
        "PostgreSQL",
        "FastAPI",
    ],
    "maxYears": 2.5,
    "minScore": 7,
    "maxResults": 25,
    "locations": ["Tel Aviv", "Israel"],
}

KEYWORDS = {
    "dev_role_keywords": {
        "fullstack": ["full stack", "fullstack", "full-stack"],
        "backend": ["backend", "back-end"],
        "frontend": ["frontend", "front-end"],
        "general": ["developer", "engineer", "programmer"],
    },
    "skill_tier1": {"react": 1.0, "typescript": 1.0, "python": 1.0, "node.js": 1.0},
    "skill_tier2": {"docker": 0.5, "postgresql": 0.5, "javascript": 0.5},
    "junior_keywords": ["junior", "entry level", "entry-level", "intern"],
    "mid_keywords": ["mid level", "mid-level"],
    "il_location_hints": ["israel", "tel aviv", "ramat gan"],
}

# ---------------------------------------------------------------------------
# _build_gemini_prompt
# ---------------------------------------------------------------------------


class TestBuildGeminiPrompt(unittest.TestCase):

    def setUp(self):
        self.jobs = [
            make_job(
                "Junior React Developer", "React TypeScript frontend role in Tel Aviv"
            ),
            make_job("Backend Engineer", "Python FastAPI backend, 2 years experience"),
        ]

    def test_prompt_contains_skills(self):
        prompt = scorer._build_gemini_prompt(self.jobs, SETTINGS)
        for skill in SETTINGS["skills"][:8]:
            self.assertIn(skill, prompt)

    def test_prompt_contains_max_years(self):
        prompt = scorer._build_gemini_prompt(self.jobs, SETTINGS)
        self.assertIn("2.5", prompt)

    def test_prompt_contains_all_job_roles(self):
        prompt = scorer._build_gemini_prompt(self.jobs, SETTINGS)
        self.assertIn("Junior React Developer", prompt)
        self.assertIn("Backend Engineer", prompt)

    def test_prompt_ids_are_sequential(self):
        prompt = scorer._build_gemini_prompt(self.jobs, SETTINGS)
        payload = json.loads(prompt.rsplit("\n\n", 1)[1])
        ids = [j["id"] for j in payload["jobs"]]
        self.assertEqual(ids, list(range(len(self.jobs))))

    def test_description_truncated_at_600(self):
        long_desc = "React TypeScript " + "x" * 700
        jobs = [make_job("Dev", long_desc)]
        prompt = scorer._build_gemini_prompt(jobs, SETTINGS)
        payload = json.loads(prompt.rsplit("\n\n", 1)[1])
        desc = payload["jobs"][0]["description"]
        self.assertLessEqual(len(desc), 605)  # 600 + "…"
        self.assertTrue(desc.endswith("…"))

    def test_rubric_bands_present(self):
        prompt = scorer._build_gemini_prompt(self.jobs, SETTINGS)
        for band in ("9-10", "7-8", "5-6", "3-4", "1-2"):
            self.assertIn(band, prompt)

    def test_prompt_ends_with_json_instruction(self):
        prompt = scorer._build_gemini_prompt(self.jobs, SETTINGS)
        self.assertIn('"scores"', prompt)
        self.assertIn("No prose", prompt)


# ---------------------------------------------------------------------------
# _algorithmic_score
# ---------------------------------------------------------------------------


class TestAlgorithmicScore(unittest.TestCase):

    def _score(self, jobs, min_score=5, max_r=25):
        s = dict(SETTINGS, minScore=min_score, maxResults=max_r)
        return scorer._algorithmic_score(jobs, s, KEYWORDS)

    def test_fullstack_react_typescript_scores_high(self):
        job = make_job(
            "Fullstack Developer", "full stack react typescript node.js junior"
        )
        result = self._score([job])
        self.assertTrue(result, "expected at least one result")
        self.assertGreaterEqual(result[0]["match_score"], 7)
        self.assertIn("fullstack", result[0]["reason"])

    def test_junior_keyword_boosts_score(self):
        base = make_job("Developer", "developer react")
        boosted = make_job("Junior Developer", "junior developer react")
        r_base = self._score([base], min_score=1)
        r_boosted = self._score([boosted], min_score=1)
        self.assertGreater(r_boosted[0]["match_score"], r_base[0]["match_score"])

    def test_unrelated_role_scores_low(self):
        job = make_job("Sales Manager", "selling enterprise software deals")
        result = self._score([job], min_score=1)
        if result:
            self.assertLessEqual(result[0]["match_score"], 4)

    def test_backend_python_scores_above_threshold(self):
        job = make_job(
            "Backend Engineer", "backend python postgresql junior entry-level"
        )
        result = self._score([job])
        self.assertTrue(result)
        self.assertIn("backend", result[0]["reason"])

    def test_results_sorted_descending(self):
        jobs = [
            make_job("Sales Rep", "cold calling"),
            make_job("Fullstack Dev", "fullstack react typescript python junior"),
            make_job("Backend Dev", "backend python postgresql"),
        ]
        result = self._score(jobs, min_score=1)
        scores = [j["match_score"] for j in result]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_max_results_respected(self):
        jobs = [
            make_job(f"Dev {i}", "fullstack react typescript junior") for i in range(20)
        ]
        result = self._score(jobs, max_r=5)
        self.assertLessEqual(len(result), 5)

    def test_drushim_no_desc_fallback(self):
        job = make_job("Fullstack Developer", description="", source="drushim")
        result = self._score([job], min_score=7)
        self.assertTrue(result, "drushim no-desc fullstack should get fallback pass")
        self.assertIn("no-desc-fallback", result[0]["reason"])

    def test_drushim_non_dev_no_fallback(self):
        job = make_job("Accountant", description="", source="drushim")
        result = self._score([job], min_score=7)
        self.assertFalse(result, "drushim non-dev with no description should not pass")

    def test_direct_board_bonus(self):
        gh_job = make_job("Developer", "developer react", source="greenhouse")
        other = make_job("Developer", "developer react", source="drushim")
        r_gh = self._score([gh_job], min_score=1)
        r_other = self._score([other], min_score=1)
        self.assertGreaterEqual(r_gh[0]["match_score"], r_other[0]["match_score"])

    def test_empty_input_returns_empty(self):
        self.assertEqual(scorer._algorithmic_score([], SETTINGS, KEYWORDS), [])


# ---------------------------------------------------------------------------
# _call_gemini (mock HTTP)
# ---------------------------------------------------------------------------


class TestCallGeminiParsing(unittest.TestCase):

    def _fake_response(self, text):
        resp_body = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        ).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_parses_clean_json(self):
        payload = json.dumps(
            {"scores": [{"id": 0, "score": 8, "reason": "good match"}]}
        )
        with patch("urllib.request.urlopen", return_value=self._fake_response(payload)):
            result = scorer._call_gemini("prompt", "fake-key", timeout=5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["score"], 8)

    def test_strips_markdown_fences(self):
        payload = (
            "```json\n"
            + json.dumps({"scores": [{"id": 0, "score": 7, "reason": "ok"}]})
            + "\n```"
        )
        with patch("urllib.request.urlopen", return_value=self._fake_response(payload)):
            result = scorer._call_gemini("prompt", "fake-key", timeout=5)
        self.assertEqual(result[0]["score"], 7)

    def test_raises_on_empty_candidates(self):
        empty = json.dumps({"candidates": []})
        with patch("urllib.request.urlopen", return_value=self._fake_response(empty)):
            with self.assertRaises(RuntimeError):
                scorer._call_gemini("prompt", "fake-key", timeout=5)

    def test_raises_on_missing_scores_key(self):
        bad = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": '{"wrong": []}'}]}}]}
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = bad.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with self.assertRaises(RuntimeError):
                scorer._call_gemini("prompt", "fake-key", timeout=5)


# ---------------------------------------------------------------------------
# score_jobs_with_llm — algorithmic fallback path (no API key)
# ---------------------------------------------------------------------------


class TestScoreJobsAlgoFallback(unittest.TestCase):

    def test_falls_back_without_api_key(self):
        jobs = [make_job("Fullstack Developer", "fullstack react typescript junior")]
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            result = scorer.score_jobs_with_llm(jobs, SETTINGS, KEYWORDS, api_key="")
        self.assertTrue(result)
        self.assertGreaterEqual(result[0]["match_score"], SETTINGS["minScore"])

    def test_empty_jobs_returns_empty(self):
        result = scorer.score_jobs_with_llm([], SETTINGS, KEYWORDS, api_key="")
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# _call_gemini error handling (404 + persistent 429 -> GeminiUnavailableError)
# ---------------------------------------------------------------------------


class TestCallGeminiUnavailable(unittest.TestCase):

    def _http_error(self, code):
        from urllib.error import HTTPError
        return HTTPError(
            "https://x", code, f"HTTP {code}", {}, None  # type: ignore[arg-type]
        )

    def test_404_raises_unavailable(self):
        # Retired/unknown model — should surface as GeminiUnavailableError so the
        # registry can advance to the next scorer rather than crashing the batch.
        with patch("urllib.request.urlopen", side_effect=self._http_error(404)):
            with self.assertRaises(scorer.GeminiUnavailableError):
                scorer._call_gemini("prompt", "fake-key", timeout=5, model="bad-model")

    def test_persistent_429_raises_unavailable(self):
        # All retries exhausted -> GeminiUnavailableError. Patch sleep so the test
        # is fast; the retry backoff inside _call_gemini calls time.sleep.
        with patch("urllib.request.urlopen", side_effect=self._http_error(429)), \
             patch("time.sleep"):
            with self.assertRaises(scorer.GeminiUnavailableError):
                scorer._call_gemini(
                    "prompt", "fake-key", timeout=5, max_429_retries=1
                )

    def test_non_404_non_429_propagates(self):
        # E.g. a 500 should bubble up as-is, not be swallowed into Unavailable.
        from urllib.error import HTTPError
        with patch("urllib.request.urlopen", side_effect=self._http_error(500)):
            with self.assertRaises(HTTPError):
                scorer._call_gemini("prompt", "fake-key", timeout=5)


# ---------------------------------------------------------------------------
# Scorer registry: SCORERS dict + score_jobs() + ScorerUnavailable chain
# ---------------------------------------------------------------------------


class TestScorerRegistry(unittest.TestCase):

    def test_registry_has_gemini_and_algorithmic(self):
        self.assertIn("gemini", scorer.SCORERS)
        self.assertIn("algorithmic", scorer.SCORERS)
        for fn in scorer.SCORERS.values():
            self.assertTrue(callable(fn))

    def test_gemini_scorer_raises_without_api_key(self):
        jobs = [make_job("Junior Developer", "react junior")]
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            with self.assertRaises(scorer.ScorerUnavailable):
                scorer.gemini_scorer(jobs, SETTINGS, KEYWORDS, api_key="")

    def test_score_jobs_falls_through_to_algorithmic(self):
        # No API key -> gemini raises ScorerUnavailable -> registry moves to
        # algorithmic, which should successfully score the obvious match.
        jobs = [make_job("Fullstack Developer", "fullstack react typescript junior")]
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            result = scorer.score_jobs(jobs, SETTINGS, KEYWORDS)
        self.assertTrue(result)
        self.assertGreaterEqual(result[0]["match_score"], SETTINGS["minScore"])

    def test_score_jobs_empty_input(self):
        self.assertEqual(scorer.score_jobs([], SETTINGS, KEYWORDS), [])

    def test_score_jobs_respects_custom_chain(self):
        # Force algorithmic-only via settings["scorers"]; gemini path should be
        # bypassed entirely (so no API key needed).
        jobs = [make_job("Fullstack Developer", "fullstack react typescript junior")]
        cfg = dict(SETTINGS, scorers=["algorithmic"])
        result = scorer.score_jobs(jobs, cfg, KEYWORDS)
        self.assertTrue(result)

    def test_score_jobs_unknown_scorer_skipped(self):
        # Garbage names in the chain are skipped, not fatal.
        jobs = [make_job("Fullstack Developer", "fullstack react typescript junior")]
        cfg = dict(SETTINGS, scorers=["nonsense", "algorithmic"])
        result = scorer.score_jobs(jobs, cfg, KEYWORDS)
        self.assertTrue(result)

    def test_score_jobs_all_unavailable_returns_empty(self):
        # If every scorer in the chain raises ScorerUnavailable, return [] rather
        # than raising — the pipeline should degrade gracefully.
        def fake_unavailable(jobs, settings, keywords=None):
            raise scorer.ScorerUnavailable("test")
        with patch.dict(scorer.SCORERS, {"fake": fake_unavailable}, clear=True):
            cfg = dict(SETTINGS, scorers=["fake"])
            result = scorer.score_jobs(
                [make_job("Dev", "react")], cfg, KEYWORDS
            )
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Integration tests — real Gemini API (skipped without GEMINI_API_KEY)
# ---------------------------------------------------------------------------

# Real-world descriptions pulled from Greenhouse IL boards (May 2026).
# GOOD = clearly junior/entry-level + matching stack; BAD = wrong seniority or wrong stack;
# MEDIUM = borderline (experience unclear, or stack mismatch, or non-dev QA type).

GOOD_JOBS = [
    # Source: Melio (Greenhouse) — real 1-2 yr fullstack role in Tel Aviv
    make_job(
        "Full Stack Engineer",
        """Melio is a B2B payments company that's transforming how businesses pay and get paid. After being \
acquired by Xero, we're integrating our payments platform with Xero's accounting software to create a unified \
financial ecosystem — and we're growing fast. Our engineering team builds and maintains infrastructure that \
processes over $100B in annual payments, and every engineer here influences the product from ideation to production.

We're looking for a motivated and curious full-stack engineer to join our Tel Aviv team. This is an entry-level \
role designed for someone with 1–2 years of experience who wants to work on a product used by hundreds of \
thousands of businesses across the US. You'll work across frontend and backend systems, shipping user-facing \
features and APIs with real business impact, surrounded by senior engineers who love mentoring.

What you'll do: Build and maintain features for Melio's payments platform using React on the frontend and Node.js \
or Python on the backend. Develop and document RESTful APIs consumed by our frontend and third-party integrations. \
Work with AWS Serverless services, TypeScript, relational and document databases, SAM CLI and CloudFormation. \
Participate in code reviews, sprint planning, and product discussions — every engineer here has a voice. \
Deploy to production regularly using our CI/CD pipelines.

What you bring: 1–2 years of experience as a full-stack engineer (or equivalent bootcamp / side-project portfolio). \
Hands-on experience with React. Ability to build backend services and REST APIs using Node.js or Python. \
Proficiency in JavaScript/TypeScript, HTML5, and CSS. A self-driven mindset — you take ownership, ask questions, \
and are always learning.

Bonus: Familiarity with cloud environments, especially AWS. Experience with relational databases (PostgreSQL, MySQL). \
Knowledge of testing frameworks (Jest, Cypress, or Playwright). AI coding tools like Cursor or GitHub Copilot.

Tech stack: React, TypeScript, Node.js, Python, AWS Serverless, PostgreSQL, DynamoDB, SAM CLI, CloudFormation.
Location: Tel Aviv (Hybrid, 3 days in office).""",
        source="greenhouse",
        company="Melio",
        location="Tel Aviv, Israel",
    ),
    # Source: Israeli fintech startup (Lever-style posting) — junior Python/FastAPI backend
    make_job(
        "Junior Backend Developer",
        """We're a fast-growing fintech startup based in Tel Aviv, building financial infrastructure for small \
and medium businesses. We're backed by top-tier VCs and growing quickly — which means you'll have real ownership \
from day one and work directly with senior engineers who care about code quality and mentorship.

We're looking for a junior backend developer with a passion for Python to join our backend team. This is an \
entry-level role — we don't expect you to know everything, but we do expect curiosity, clean code, and a drive \
to grow. You'll work in our modern Python stack using FastAPI, PostgreSQL, and Docker, and you'll help build the \
APIs and microservices that power our core product. This role is perfect for a recent graduate, a bootcamp alumnus, \
or someone with 0–2 years of experience who wants to level up fast in a real production environment.

Responsibilities: Build and maintain RESTful APIs using Python and FastAPI. Write clean, well-tested code and \
participate in code reviews. Collaborate with the frontend team to define and integrate API contracts. Help \
maintain PostgreSQL schemas and write efficient queries. Contribute to our Docker-based deployment setup. \
Participate in sprint planning and async standups with a small, tight-knit engineering team.

Requirements: 0–2 years of professional experience (or strong personal/open-source projects). Good Python \
fundamentals. Understanding of REST API concepts. Basic SQL knowledge. Eagerness to learn Docker, cloud \
deployment, and modern API patterns.

Nice to have: Experience with FastAPI or async Python. PostgreSQL or another RDBMS. Any cloud experience \
(AWS, GCP). Contributions to open source or a personal project you're proud of.

We offer: Competitive salary + equity, health insurance, Keren Hishtalmut, pension, hybrid work 3 days from \
our Tel Aviv office, monthly team events, learning budget, and a senior team that genuinely invests in \
growing junior developers.
Location: Tel Aviv, Israel.""",
        source="greenhouse",
        company="FinFlow",
        location="Tel Aviv, Israel",
    ),
    # Source: Israeli SaaS startup — entry-level fullstack (React + Node.js, Ramat Gan)
    make_job(
        "Fullstack Developer (Entry Level)",
        """At Workly, we're building the next generation of workforce management software used by 2,000+ \
businesses across Israel and expanding internationally. Our Tel Aviv R&D team is 25 engineers strong and \
we're looking for a junior fullstack developer to help us ship features faster and better.

We believe in growing talent from within. If you're a recent CS graduate or bootcamp grad who loves building \
things with React and Node.js, this is a great place to start your career. You'll be paired with a senior \
buddy for your first 90 days, participate in weekly tech talks, and have real code shipped to production within \
your first month.

What the role involves: Building new features across the full stack — React components on the frontend, \
Express/Node.js APIs on the backend. Working with TypeScript throughout the codebase. Writing unit and \
integration tests with Jest. Participating in design reviews and giving feedback on product mock-ups. \
Fixing bugs, improving performance, and occasionally diving into infrastructure (Docker, CI/CD pipelines).

Who we're looking for: A developer with 0–1 years of experience, or a strong portfolio of personal or \
bootcamp projects. Solid understanding of JavaScript and TypeScript. Some React experience (even from \
personal projects). Curiosity about the full stack — not afraid to jump between frontend and backend. \
Good English communication.

Our stack: React 18, TypeScript, Node.js, Express, PostgreSQL, Redis, Docker, AWS ECS, GitHub Actions.
Location: Ramat Gan (2 min from Savidor station). Hybrid — 4 days in office.""",
        source="greenhouse",
        company="Workly",
        location="Ramat Gan, Israel",
    ),
]

BAD_JOBS = [
    # Source: HoneyBook (Greenhouse) — real senior backend role, 7+ years, Tel Aviv
    make_job(
        "Backend Engineer - Financial Platform",
        """HoneyBook is an AI-powered business management platform built for independent business owners \
and service-based entrepreneurs. Founded in 2013 and trusted by over 100,000 businesses across the US and \
Canada, HoneyBook helps freelancers and small business owners manage clients, bookings, contracts, and payments \
— all in one place. Our mission: empower independent business owners to build thriving businesses.

The Financial Platform team at HoneyBook builds the systems that power money movement from checkout to payout. \
We ensure funds are processed securely, reliably, and at scale, enabling our members to get paid faster and \
with confidence. This team sits at the intersection of product and infrastructure — our work directly impacts \
every transaction on the platform.

We're looking for an experienced Backend Engineer to join the Financial Platform team in Tel Aviv. You'll \
design, build, and operate reliable, resilient, and observable services handling real-money flows. You'll lead \
complex technical initiatives, set the technical direction, and raise the bar for reliability and engineering \
quality across the team. If you thrive in environments with real stakes and love building systems that just \
don't fail — this is the role for you.

What you'll do: Architect and implement highly reliable backend systems for payment processing and money \
movement at scale. Lead the design and technical direction for new platform capabilities. Drive observability, \
incident response, and post-mortem culture. Mentor engineers and participate in technical hiring. Collaborate \
with product, design, and cross-functional engineering teams to shape and execute the roadmap.

What we're looking for: 7+ years of software engineering experience with a strong backend focus. Proven track \
record building and operating production systems at scale under strict reliability requirements. Deep understanding \
of distributed system design, fault-tolerance patterns, and database internals. Experience with financial systems, \
payment flows, or regulated domains is a strong plus. Excellent communication and ability to influence technical \
direction across teams. Values: People Come First, Raise the Bar, Own It.
Location: Tel Aviv (Hybrid).""",
        source="greenhouse",
        company="HoneyBook",
        location="Tel Aviv, Israel",
    ),
    # Source: Gong (Greenhouse) — real senior backend role, 5+ years, Java/Spring
    make_job(
        "Backend Engineer, Communications Capture",
        """Gong transforms revenue organizations by harnessing customer interactions to increase business \
efficiency, improve decision-making, and accelerate revenue growth. Our Revenue Intelligence Platform uses \
proprietary AI technology to enable go-to-market teams to capture, understand, and act on all customer \
interactions in a single integrated platform. More than 4,000 companies worldwide rely on Gong — including \
LinkedIn, HubSpot, and Slack.

The Communications Capture team manages Gong's core data pipeline — the backbone that ingests customer \
interactions from external communication systems: email, calendar, CRM, video conferencing platforms, and \
more. These systems are "online" in the sense that they are used by all customers at all times and form the \
foundation for Gong's entire product suite. The work is technically complex, latency-sensitive, and directly \
user-facing.

We're hiring a Backend Engineer for our Dublin R&D hub. This is a senior-leaning role for someone who can \
own large technical domains, make architectural decisions independently, and help raise engineering quality \
across the team.

What you'll do: Design and implement scalable, high-performance backend services using Java, Spring Boot, and \
cloud technologies. Build integration infrastructure and connectors for third-party applications (CRMs, email \
providers, conferencing platforms). Build generic, modular, reusable API interfaces supporting diverse use cases. \
Enhance system security for external-facing integrations. Collaborate with internal teams and external partners. \
Take full ownership of features from conception through production deployment.

What we're looking for: 5+ years of backend software development with a strong Java/JVM foundation. \
Experience with distributed, event-driven architectures (Kafka, SQS, or similar). Proficiency in \
Spring Boot and designing robust REST/gRPC APIs. AWS or GCP cloud platform experience. Deep understanding \
of secure API design and authentication patterns. You write production-ready code and take full ownership \
of what you ship.
Location: Dublin, Ireland (Hybrid — 3 days office, 2 remote).""",
        source="greenhouse",
        company="Gong",
        location="Dublin, Ireland",
    ),
    # Source: Lemonade (Ashby) — real senior backend role, 6+ years, TypeScript/NestJS, Tel Aviv
    make_job(
        "Senior Backend Engineer",
        """Lemonade is a fully licensed and regulated insurance company built on a digital-native foundation. \
We've rebuilt insurance from scratch using AI, behavioral economics, and a Certified B Corp model to make \
insurance delightful, honest, and socially impactful. We serve millions of customers across the US, EU, and \
Israel, and we're still growing fast.

We're looking for a Senior Backend Engineer to join our Tel Aviv engineering team and work on our core \
insurance platform. You'll be part of a product-focused squad building the microservices that power \
Lemonade's policies, claims, and payments — at scale, with reliability requirements that don't forgive \
sloppiness.

This is a senior role. We expect you to own architectural decisions, mentor junior engineers, lead code \
reviews, and drive technical excellence across your squad. If you're looking to grow into senior, this \
isn't the right role — if you're already there and want real ownership over critical systems, read on.

What you'll do: Design and implement complex microservices using TypeScript and NestJS. Own backend systems \
end-to-end from architecture review through deployment and monitoring. Mentor junior and mid-level engineers. \
Collaborate with product managers, designers, and engineering leads to define technical direction. Drive \
reliability, observability, and performance improvements across the platform.

What you'll bring: 6+ years of backend software engineering experience. Strong TypeScript/Node.js and \
hands-on NestJS experience. Microservices architecture and distributed systems at production scale. Deep \
AWS infrastructure knowledge. PostgreSQL or equivalent RDBMS at scale. Proven track record of mentoring \
engineers and leading technical initiatives. Passion for AI tools and enthusiasm for integrating them \
into daily work.

We offer: Competitive salary + meaningful equity, Keren Hishtalmut, pension, private health insurance, \
flexible hybrid work from our Tel Aviv offices.
Location: Tel Aviv, Israel.""",
        source="greenhouse",
        company="Lemonade",
        location="Tel Aviv, Israel",
    ),
    # Source: SimilarWeb (Greenhouse) — Android dev, 3+ yrs, completely wrong platform
    make_job(
        "Android Developer",
        """Similarweb is the leading digital intelligence platform, used by over 3,500 customers in 50+ \
countries — from Fortune 500 companies to fast-growing startups — to make smarter decisions about their \
digital strategy. We turn the internet's data into actionable market intelligence: web traffic, audience \
insights, competitive benchmarks, and more. Our Tel Aviv office is our global R&D hub.

We're hiring an Android Developer to join our mobile team in Tel Aviv. The mobile team designs and ships \
high-quality B2C apps used by millions of paying and non-paying users. You'll work on challenging product \
problems that require creative thinking, engineering rigor, and a deep appreciation for user experience. \
You'll evaluate technical feasibility and trade-offs, contribute to team velocity and quality through code \
reviews and mentoring, and collaborate closely with product managers, designers, and backend engineers.

What we're looking for: B.Sc. or M.Sc. in Computer Science or equivalent. 3+ years of software development \
experience with at least 2 years in native Android development using Java or Kotlin. A published app on \
the Google Play Store. Strong experience with Jetpack Compose and Kotlin coroutines. High English \
proficiency (written and spoken). Product-minded — you care about the user experience, not just the code.

Advantages: Experience with iOS development or cross-platform frameworks (React Native, Flutter). \
Familiarity with Node.js or AWS cloud services. CI/CD pipeline experience (GitHub Actions, Bitrise). \
Experience with performance profiling and memory optimization.

Location: Tel Aviv-Yafo, Israel (Hybrid — 3 days in office).""",
        source="greenhouse",
        company="SimilarWeb",
        location="Tel Aviv, Israel",
    ),
    # Source: WalkMe (Lever) — real non-dev sales role (Account Executive)
    make_job(
        "Account Executive - Enterprise DACH",
        """At WalkMe, now an SAP company, we're not just the leader in digital adoption — we started the \
digital adoption revolution. WalkMe's platform enables organizations to pinpoint and resolve digital friction, \
regain control of their technology stack, and be better equipped to manage change. With over 1,600 clients, \
including 55 Fortune 100 companies and 6 Fortune 10 companies, we're transforming how enterprises interact \
with their technology daily.

We're seeking a driven and experienced Account Executive to join our Enterprise Sales team, focusing on the \
DACH region (Germany, Austria, Switzerland). You'll manage all aspects of the sales cycle — from prospecting \
and relationship-building through to contract negotiation and close. This is a quota-carrying, hunter role \
for someone who thrives in complex enterprise sales environments with long deal cycles.

What You'll Own: Drive end-to-end sales process for enterprise accounts across DACH. Develop and manage a \
healthy pipeline of new logo opportunities alongside expansions in existing accounts. Build executive \
relationships at C-suite and VP level at Fortune 500 companies. Accurately forecast pipeline and revenue \
in Salesforce. Partner with Customer Success, BDRs, Marketing, and Alliances to identify and close deals. \
Negotiate and close complex, multi-stakeholder enterprise contracts.

What You Need to Succeed: Extensive experience in an enterprise SaaS Account Executive role (5+ years). \
A proven track record of consistent quota attainment and over-achievement. Experience selling into DACH \
enterprise accounts; financial services industry a plus. Ability to manage a long, strategic sales cycle \
from first meeting through signature. Native or near-native German required; fluent English required. \
Salesforce CRM proficiency. Strong executive presence and presentation skills.

Location: Germany (Remote). Compensation: Base + uncapped commission + equity.""",
        source="greenhouse",
        company="WalkMe",
        location="Germany (Remote)",
    ),
]

MEDIUM_JOBS = [
    # Source: AppsFlyer (Greenhouse) — 4+ years required, no "senior" title, right stack + Israel
    # Gemini should score 4-7: good tech match but above junior experience threshold
    make_job(
        "Software Engineer - Audience Platform",
        """AppsFlyer is a global leader in marketing measurement, attribution, and data collaboration. \
Our platform processes hundreds of billions of data points every day, helping brands make better business \
decisions with privacy-first technologies at massive scale. We operate from 25 offices across 19 countries, \
and our Herzliya R&D center is one of our largest engineering hubs.

The Collab Unit is looking for a Software Engineer to join the Audience Team in Herzliya. The Audience Team \
builds the products and infrastructure that enable customers to create, manage, and activate high-value \
audiences across multiple channels and marketing use cases. This is a strategic domain at the heart of how \
customers derive value from AppsFlyer's behavioral data.

What you'll do: Design, build, and maintain scalable backend systems and data flows for audience creation, \
enrichment, segmentation, and activation. Own features end-to-end — from design through development, testing, \
deployment, and monitoring. Collaborate with product, architecture, data, and engineering teams to define and \
execute the team roadmap. Write clean, maintainable, production-ready code. Contribute to architecture \
discussions, design reviews, and code reviews. Continuously improve development practices and engineering \
processes.

What you have: 4+ years of software development experience, with strong backend engineering skills. \
Proven experience building and operating production-grade distributed systems. Strong understanding of \
software design, system architecture, and development best practices. Strong problem-solving skills and \
ability to work effectively with ambiguous requirements. B.Sc. in Computer Science or equivalent.

Bonus: Experience with audience segmentation, campaign targeting, or user data platforms. Large-scale \
data systems or real-time pipeline background. Recommended by an AppsFlyer employee.
Location: Herzliya, Israel (Hybrid).""",
        source="greenhouse",
        company="AppsFlyer",
        location="Herzliya, Israel",
    ),
]


@unittest.skipUnless(
    os.getenv("GEMINI_API_KEY"), "GEMINI_API_KEY not set — skipping live Gemini tests"
)
class TestGeminiIntegration(unittest.TestCase):
    """Live Gemini API tests. All sample jobs are sent in a single batched call."""

    @classmethod
    def setUpClass(cls):
        all_jobs = GOOD_JOBS + BAD_JOBS + MEDIUM_JOBS
        # Use a low minScore so we get scores for everything
        test_settings = dict(SETTINGS, minScore=1, maxResults=100)
        cls.results = scorer.score_jobs_with_llm(all_jobs, test_settings, KEYWORDS)
        cls.by_role = {j["role"]: j for j in cls.results}

    def _score(self, role):
        job = self.by_role.get(role)
        self.assertIsNotNone(job, f"Role not found in results: {role!r}")
        return job["match_score"]

    def test_good_jobs_score_high(self):
        for job in GOOD_JOBS:
            s = self._score(job["role"])
            self.assertGreaterEqual(
                s, 7, f"Expected ≥7 for GOOD job {job['role']!r}, got {s}"
            )

    def test_bad_jobs_score_low(self):
        # Must all fall below the production minScore=7 threshold; allow up to 6
        # (e.g. over-experienced IL roles land at 5-6, not 1-2, per the 5-band rubric)
        for job in BAD_JOBS:
            s = self._score(job["role"])
            self.assertLessEqual(
                s, 6, f"Expected ≤6 for BAD job {job['role']!r}, got {s}"
            )

    def test_medium_jobs_in_range(self):
        # Borderline jobs: not rejected outright (not 0-2) but not a clear pass either
        for job in MEDIUM_JOBS:
            s = self._score(job["role"])
            self.assertGreaterEqual(
                s, 3, f"Expected ≥3 for MEDIUM job {job['role']!r}, got {s}"
            )
            self.assertLessEqual(
                s, 8, f"Expected ≤8 for MEDIUM job {job['role']!r}, got {s}"
            )

    def test_all_results_have_reason(self):
        for job in self.results:
            self.assertTrue(job.get("reason"), f"Missing reason for {job['role']!r}")

    def test_results_sorted_descending(self):
        scores = [j["match_score"] for j in self.results]
        self.assertEqual(scores, sorted(scores, reverse=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
