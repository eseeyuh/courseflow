# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Two further Northbridge courses (``supplement.yaml``): their documents,
and what each assessment brief states, as data.

AIF7002 AI Foundations in Practice and DVS7003 Data Visualisation fill out
the demo workspace: two assessments each, with weightings, deadlines,
requirements stated in prose and the teaching material that covers them. They hold
no conflict, hidden obligation or change; those stay in the two main demo
courses. Everything is synthetic, like the rest of Northbridge.

They are not part of the frozen evaluation corpus and have no golden labels.
``BRIEFS`` is what their extraction answers with (``supplement``): authored,
not model output, and checked by the same evidence gates. Every quote must
occur in the rendered document (``tests/test_demo_supplement.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.demo.northbridge_content import (
    Document,
    DocxDoc,
    DocxItem,
    PdfDoc,
    PptxDoc,
    Slide,
    _h,
    _p,
    _t,
)

# --- AIF7002 AI Foundations in Practice ---------------------------------------------------

AIF_CASE_STUDY_BRIEF = PdfDoc(
    title="AIF7002 Assessment 1: AI Systems Case Study",
    pages=(
        _p(
            _t("AIF7002 AI Foundations in Practice"),
            _t("Assessment 1: AI Systems Case Study"),
            "School of Computing, Northbridge University (2026-27, Teaching Block 1)",
            "",
            _h("Key information"),
            "Weighting: 40% of the module mark.",
            "Submission deadline: Wednesday 18 November 2026, 12:00 (UK time).",
            "Submit a single PDF through the Assessment 1 submission point on the module page.",
            "Word limit: 2,000 words, excluding references and appendices.",
            "Module leader: Dr Lena Moreau (l.moreau@northbridge.example).",
            "",
            _h("Overview"),
            "Choose one AI system that is already in use, such as a triage chatbot, a fraud",
            "detection service or a route-planning assistant, and analyse it as a whole",
            "system: the model, the data it depends on, the people who use it and the people",
            "it affects.",
        ),
        _p(
            _h("The task"),
            "Your case study should open by identifying the AI system, the task it performs and",
            "the people it is intended to serve. Describe the system as it is actually used",
            "rather than as it is marketed, drawing only on publicly available information; do",
            "not contact the organisation.",
            "",
            "The core of the case study is analytical. You should discuss the limitations of the",
            "underlying model and explain when its outputs should not be trusted, using concrete",
            "examples where you can find them. Building on that analysis, you must evaluate the",
            "ethical and operational risks the system creates for the people who use it or are",
            "affected by it, and weigh how likely and how serious each risk is.",
            "",
            "Finally, you should propose practical safeguards that would reduce the most serious",
            "of those risks. Strong answers explain who would carry out each safeguard and what",
            "it would cost the organisation, rather than listing good intentions.",
            "",
            _h("Assessment criteria"),
            "Marks are allocated to the description of the system and its intended users (20%),",
            "the analysis of model limitations (25%), the evaluation of risk (30%), the proposed",
            "safeguards (20%), and presentation and referencing (5%).",
            "",
            _h("Support"),
            "The Week 2 lecture and the risk workshop cover the material for this case study.",
        ),
    ),
)

AIF_REFLECTION_BRIEF = PdfDoc(
    title="AIF7002 Assessment 2: Applied AI Reflection",
    pages=(
        _p(
            _t("AIF7002 AI Foundations in Practice"),
            _t("Assessment 2: Applied AI Reflection"),
            "School of Computing, Northbridge University (2026-27, Teaching Block 1)",
            "",
            _h("Key information"),
            "Weighting: 60% of the module mark.",
            "Submission deadline: Thursday 26 November 2026, 16:00 (UK time).",
            "Submit a single PDF through the Assessment 2 submission point on the module page.",
            "Word limit: 3,000 words, excluding references and appendices.",
            "",
            _h("Overview"),
            "An organisation of your choice wants to automate one decision it makes today",
            "by hand. Reflect on how it should do so, drawing on the whole module.",
        ),
        _p(
            _h("The task"),
            "Your reflection should compare two different AI approaches to the same problem, for",
            "example a rule-based system and a learned model, and consider what each would",
            "demand of the organisation in data, expertise and maintenance. On the strength of",
            "that comparison, justify the implementation choice you would recommend to the",
            "organisation, acknowledging what it gives up.",
            "",
            "A recommendation is only as good as the evidence behind it, so explain how the",
            "chosen system should be evaluated before launch and monitored after deployment.",
            "Your reflection must close by considering what responsible deployment would demand",
            "of the organisation: who remains accountable for its decisions, and how those",
            "affected could challenge them.",
            "",
            _h("Assessment criteria"),
            "Marks are allocated to the comparison of approaches (25%), the justification of the",
            "recommended choice (25%), evaluation and monitoring (25%), responsible deployment",
            "(20%), and presentation and referencing (5%).",
        ),
    ),
)

AIF_WEEK_02 = PptxDoc(
    title="Week 2: From Models to AI Systems",
    slides=(
        Slide("Week 2: From Models to AI Systems", ("AIF7002 AI Foundations in Practice",)),
        Slide(
            "A model is not a system",
            (
                "A deployed AI system combines a model, data pipelines, an interface and people",
                "Most failures happen around the model, not inside it",
            ),
        ),
        Slide(
            "Who is the system for?",
            (
                "Name the task the system performs and its intended users",
                "Separate the people who operate it from the people it affects",
            ),
        ),
        Slide(
            "Where models fall short",
            (
                "Models fail on cases unlike the data they were trained on",
                "A confident output can still be wrong",
                "Know when an output should not be trusted, and who checks it",
            ),
            notes="Use the loan-scoring example from the reading.",
        ),
        Slide(
            "Rule-based or learned?",
            (
                "Rule-based systems are transparent but brittle",
                "Learned models adapt to data but are harder to explain",
                "Language models are flexible but can produce fluent errors",
            ),
        ),
        Slide(
            "Choosing an approach",
            (
                "Start from the problem, the data and the cost of a mistake",
                "Prefer the simplest approach that meets the need",
                "Write down why the alternatives were rejected",
            ),
        ),
    ),
)

AIF_WEEK_03 = PptxDoc(
    title="Week 3: Responsible AI in Practice",
    slides=(
        Slide("Week 3: Responsible AI in Practice", ("AIF7002 AI Foundations in Practice",)),
        Slide(
            "Harms to people",
            (
                "Unfair outcomes for some groups",
                "Privacy loss when data is reused",
                "Over-reliance on automated decisions",
            ),
        ),
        Slide(
            "Evaluating before launch",
            (
                "Test on data that represents the people affected",
                "Report error rates per group, not only overall",
                "Agree acceptance criteria before testing starts",
            ),
        ),
        Slide(
            "Monitoring after deployment",
            (
                "Track input drift and outcome quality over time",
                "Give users a way to report problems",
                "Decide in advance who acts when an alert fires",
            ),
        ),
        Slide(
            "Responsible deployment",
            (
                "Assign a named owner for the system",
                "Keep a human able to override its decisions",
                "Publish what the system does and does not do",
            ),
        ),
    ),
)

AIF_RISK_WORKSHOP = DocxDoc(
    title="Workshop: Evaluating AI Risks",
    items=(
        DocxItem("Workshop: Evaluating AI Risks", "title"),
        DocxItem(
            "Work in groups of three on the AI system you have chosen for your case study. "
            "Bring your notes from the Week 2 lecture."
        ),
        DocxItem("Part 1: Map the risks", "heading1"),
        DocxItem(
            "List the ethical and operational risks of the system: who could be harmed, "
            "how, and how often. Rate each risk by likelihood and severity."
        ),
        DocxItem("Part 2: Design safeguards", "heading1"),
        DocxItem(
            "For the two most serious risks, propose a practical safeguard: a human review "
            "step, a usage limit, a fallback, or a clearer interface. Check that the "
            "organisation could realistically run it."
        ),
    ),
)

# --- DVS7003 Data Visualisation -----------------------------------------------------------

DVS_CRITIQUE_BRIEF = PdfDoc(
    title="DVS7003 Assessment 1: Visualisation Critique",
    pages=(
        _p(
            _t("DVS7003 Data Visualisation"),
            _t("Assessment 1: Visualisation Critique"),
            "School of Computing, Northbridge University (2026-27, Teaching Block 1)",
            "",
            _h("Key information"),
            "Weighting: 35% of the module mark.",
            "Submission deadline: Monday 16 November 2026, 16:00 (UK time).",
            "Submit a single PDF through the Assessment 1 submission point on the module page.",
            "Word limit: 1,500 words, plus your redesign sketch.",
            "Module leader: Dr Tomas Reyes (t.reyes@northbridge.example).",
            "",
            _h("Overview"),
            "Choose one published chart or dashboard from a news site or a public report",
            "and critique it as a piece of communication.",
        ),
        _p(
            _h("The task"),
            "Your critique should first establish the intended audience of the visualisation and",
            "the decision it is meant to help them make, since every later judgement depends on",
            "it. With that in mind, you should evaluate the visual encoding choices, including",
            "the marks, channels, scales and layout, and explain how well each serves the reader.",
            "",
            "Many published charts mislead without intending to. You are required to discuss any",
            "misleading or ineffective design you find, such as a truncated axis or a poor colour",
            "scale, and to explain what a reader is likely to conclude as a result. Your critique",
            "should end by recommending specific improvements, accompanied by a sketch of your",
            "redesign; a photographed hand drawing is acceptable.",
            "",
            _h("Assessment criteria"),
            "Marks are allocated to audience and purpose (15%), the analysis of encoding (30%),",
            "misleading or ineffective design (30%), and the improvements and redesign (25%).",
        ),
    ),
)

DVS_DASHBOARD_BRIEF = PdfDoc(
    title="DVS7003 Assessment 2: Interactive Dashboard Project",
    pages=(
        _p(
            _t("DVS7003 Data Visualisation"),
            _t("Assessment 2: Interactive Dashboard Project"),
            "School of Computing, Northbridge University (2026-27, Teaching Block 1)",
            "",
            _h("Key information"),
            "Weighting: 65% of the module mark.",
            "Submission deadline: Tuesday 24 November 2026, 23:59 (UK time).",
            "Submit a link to your dashboard and a design document of up to 2,000 words.",
            "",
            _h("Overview"),
            "Build an interactive dashboard that helps a named group of users answer",
            "three to five questions about a public dataset of your choice.",
        ),
        _p(
            _h("The task"),
            "The dashboard itself should be an interactive visualisation that lets users explore",
            "the dataset rather than a set of static charts placed side by side. For each",
            "question it answers, you must justify the chart type you selected over the obvious",
            "alternatives.",
            "",
            "Users should never have to search for what matters, so create a clear visual",
            "hierarchy in which the most important measure is seen first. The accompanying",
            "design document should record your design decisions and the alternatives you",
            "rejected, with a sentence on why each was rejected.",
            "",
            _h("Assessment criteria"),
            "Marks are allocated to interaction design (25%), chart selection (25%), visual",
            "hierarchy (20%) and the design document (30%).",
        ),
    ),
)

DVS_WEEK_02 = PptxDoc(
    title="Week 2: Visual Encoding",
    slides=(
        Slide("Week 2: Visual Encoding", ("DVS7003 Data Visualisation",)),
        Slide(
            "Marks and channels",
            (
                "Marks are the shapes that stand for data: points, lines and bars",
                "Channels such as position, length and colour encode the values",
            ),
        ),
        Slide(
            "Ranking the channels",
            (
                "Position on a common scale is read most accurately",
                "Area and colour saturation are read least accurately",
                "Use the strongest channel for the most important variable",
            ),
        ),
        Slide(
            "Start with the audience",
            (
                "Who reads the chart, and what decision will it support?",
                "An expert audience tolerates denser encodings than the general public",
            ),
        ),
    ),
)

DVS_WEEK_03 = PptxDoc(
    title="Week 3: Perception and Colour",
    slides=(
        Slide("Week 3: Perception and Colour", ("DVS7003 Data Visualisation",)),
        Slide(
            "How charts mislead",
            (
                "A truncated axis exaggerates small differences",
                "Dual axes invite false comparisons",
                "3D effects distort area and angle",
            ),
        ),
        Slide(
            "Choosing a colour scale",
            (
                "Use a sequential scale for ordered values",
                "Use a diverging scale around a meaningful midpoint",
                "Avoid rainbow scales: they create boundaries that are not in the data",
            ),
        ),
        Slide(
            "Redesigning a chart",
            (
                "Fix the encoding before the styling",
                "Remove decoration that carries no data",
                "Sketch the alternative before you build it",
            ),
        ),
    ),
)

DVS_WEEK_05 = PptxDoc(
    title="Week 5: Dashboard Design",
    slides=(
        Slide("Week 5: Dashboard Design", ("DVS7003 Data Visualisation",)),
        Slide(
            "One question per view",
            (
                "Each chart should answer one question its users ask",
                "Match the chart type to the question: trend, comparison, share or distribution",
            ),
        ),
        Slide(
            "Visual hierarchy",
            (
                "Place the most important measure where the eye lands first",
                "Use size and contrast to signal importance",
                "Group related views and align them",
            ),
        ),
        Slide(
            "Common dashboard mistakes",
            (
                "Too many views compete for attention",
                "Related charts use inconsistent scales",
            ),
        ),
    ),
)

DVS_WEEK_06 = PptxDoc(
    title="Week 6: Interaction and Storytelling",
    slides=(
        Slide("Week 6: Interaction and Storytelling", ("DVS7003 Data Visualisation",)),
        Slide(
            "Why interact?",
            (
                "Overview first, then zoom and filter, then details on demand",
                "Interaction should let users explore a follow-up question, not decorate",
            ),
        ),
        Slide(
            "Linking views",
            (
                "Brushing in one view highlights the same records in the others",
                "A filter must show clearly what is currently selected",
            ),
        ),
        Slide(
            "Telling the story",
            (
                "Annotate the insight you want the reader to notice",
                "Record each design decision and the alternative you rejected",
            ),
        ),
    ),
)

# --- the files ----------------------------------------------------------------------------

# Path inside demo_data/northbridge -> document.
SUPPLEMENT_DOCUMENTS: dict[str, Document] = {
    "courses/aif7002/AIF_Case_Study_Brief.pdf": AIF_CASE_STUDY_BRIEF,
    "courses/aif7002/AIF_Reflection_Brief.pdf": AIF_REFLECTION_BRIEF,
    "courses/aif7002/AIF_Week_02_Models_to_Systems.pptx": AIF_WEEK_02,
    "courses/aif7002/AIF_Week_03_Responsible_AI.pptx": AIF_WEEK_03,
    "courses/aif7002/AIF_Workshop_AI_Risks.docx": AIF_RISK_WORKSHOP,
    "courses/dvs7003/DVS_Critique_Brief.pdf": DVS_CRITIQUE_BRIEF,
    "courses/dvs7003/DVS_Dashboard_Brief.pdf": DVS_DASHBOARD_BRIEF,
    "courses/dvs7003/DVS_Week_02_Visual_Encoding.pptx": DVS_WEEK_02,
    "courses/dvs7003/DVS_Week_03_Perception_Colour.pptx": DVS_WEEK_03,
    "courses/dvs7003/DVS_Week_05_Dashboard_Design.pptx": DVS_WEEK_05,
    "courses/dvs7003/DVS_Week_06_Interaction.pptx": DVS_WEEK_06,
}


# --- what each brief states ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Requirement:
    """One requirement a brief states (the sentence that states it), and where
    the course teaches it: each (teaching resource title, a phrase of the
    slide or section) pair."""

    statement: str  # as the brief states it, whitespace collapsed
    taught_in: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Brief:
    """An assessment brief's stated facts, each with its exact source quote."""

    course: str
    title: str
    title_quote: str
    weighting: int
    weighting_quote: str
    due_date: str
    due_time: str
    due_quote: str
    requirements: tuple[Requirement, ...]


_AIF2 = "Week 2: From Models to AI Systems"
_AIF3 = "Week 3: Responsible AI in Practice"
_AIFW = "Workshop: Evaluating AI Risks"
_DVS2 = "Week 2: Visual Encoding"
_DVS3 = "Week 3: Perception and Colour"
_DVS5 = "Week 5: Dashboard Design"
_DVS6 = "Week 6: Interaction and Storytelling"

BRIEFS: tuple[Brief, ...] = (
    Brief(
        course="aif7002",
        title="AI Systems Case Study",
        title_quote="Assessment 1: AI Systems Case Study",
        weighting=40,
        weighting_quote="Weighting: 40% of the module mark.",
        due_date="2026-11-18",
        due_time="12:00",
        due_quote="Submission deadline: Wednesday 18 November 2026, 12:00 (UK time).",
        requirements=(
            Requirement(
                "Your case study should open by identifying the AI system, the task it performs and "
                "the people it is intended to serve.",
                ((_AIF2, "Name the task the system performs and its intended users"),),
            ),
            Requirement(
                "You should discuss the limitations of the underlying model and explain when its "
                "outputs should not be trusted, using concrete examples where you can find them.",
                ((_AIF2, "Know when an output should not be trusted"),),
            ),
            Requirement(
                "Building on that analysis, you must evaluate the ethical and operational risks "
                "the system creates for the people who use it or are affected by it, and weigh "
                "how likely and how serious each risk is.",
                ((_AIFW, "List the ethical and operational risks of the system"),),
            ),
            Requirement(
                "Finally, you should propose practical safeguards that would reduce the most "
                "serious of those risks.",
                ((_AIFW, "propose a practical safeguard"),),
            ),
        ),
    ),
    Brief(
        course="aif7002",
        title="Applied AI Reflection",
        title_quote="Assessment 2: Applied AI Reflection",
        weighting=60,
        weighting_quote="Weighting: 60% of the module mark.",
        due_date="2026-11-26",
        due_time="16:00",
        due_quote="Submission deadline: Thursday 26 November 2026, 16:00 (UK time).",
        requirements=(
            Requirement(
                "Your reflection should compare two different AI approaches to the same problem, "
                "for example a rule-based system and a learned model, and consider what each "
                "would demand of the organisation in data, expertise and maintenance.",
                ((_AIF2, "Rule-based systems are transparent but brittle"),),
            ),
            Requirement(
                "On the strength of that comparison, justify the implementation choice you would "
                "recommend to the organisation, acknowledging what it gives up.",
                ((_AIF2, "Prefer the simplest approach that meets the need"),),
            ),
            Requirement(
                "A recommendation is only as good as the evidence behind it, so explain how the "
                "chosen system should be evaluated before launch and monitored after deployment.",
                (
                    (_AIF3, "Test on data that represents the people affected"),
                    (_AIF3, "Track input drift and outcome quality over time"),
                ),
            ),
            Requirement(
                "Your reflection must close by considering what responsible deployment would "
                "demand of the organisation: who remains accountable for its decisions, and how "
                "those affected could challenge them.",
                ((_AIF3, "Keep a human able to override its decisions"),),
            ),
        ),
    ),
    Brief(
        course="dvs7003",
        title="Visualisation Critique",
        title_quote="Assessment 1: Visualisation Critique",
        weighting=35,
        weighting_quote="Weighting: 35% of the module mark.",
        due_date="2026-11-16",
        due_time="16:00",
        due_quote="Submission deadline: Monday 16 November 2026, 16:00 (UK time).",
        requirements=(
            Requirement(
                "Your critique should first establish the intended audience of the visualisation "
                "and the decision it is meant to help them make, since every later judgement "
                "depends on it.",
                ((_DVS2, "Who reads the chart, and what decision will it support?"),),
            ),
            Requirement(
                "With that in mind, you should evaluate the visual encoding choices, including the "
                "marks, channels, scales and layout, and explain how well each serves the reader.",
                (
                    (_DVS2, "Channels such as position, length and colour encode the values"),
                    (_DVS2, "Position on a common scale is read most accurately"),
                ),
            ),
            Requirement(
                "You are required to discuss any misleading or ineffective design you find, such as "
                "a truncated axis or a poor colour scale, and to explain what a reader is likely "
                "to conclude as a result.",
                ((_DVS3, "A truncated axis exaggerates small differences"),),
            ),
            Requirement(
                "Your critique should end by recommending specific improvements, accompanied by a "
                "sketch of your redesign; a photographed hand drawing is acceptable.",
                ((_DVS3, "Sketch the alternative before you build it"),),
            ),
        ),
    ),
    Brief(
        course="dvs7003",
        title="Interactive Dashboard Project",
        title_quote="Assessment 2: Interactive Dashboard Project",
        weighting=65,
        weighting_quote="Weighting: 65% of the module mark.",
        due_date="2026-11-24",
        due_time="23:59",
        due_quote="Submission deadline: Tuesday 24 November 2026, 23:59 (UK time).",
        requirements=(
            Requirement(
                "The dashboard itself should be an interactive visualisation that lets users "
                "explore the dataset rather than a set of static charts placed side by side.",
                ((_DVS6, "Overview first, then zoom and filter, then details on demand"),),
            ),
            Requirement(
                "For each question it answers, you must justify the chart type you selected over "
                "the obvious alternatives.",
                ((_DVS5, "Match the chart type to the question"),),
            ),
            Requirement(
                "Users should never have to search for what matters, so create a clear visual "
                "hierarchy in which the most important measure is seen first.",
                ((_DVS5, "Place the most important measure where the eye lands first"),),
            ),
            Requirement(
                "The accompanying design document should record your design decisions and the "
                "alternatives you rejected, with a sentence on why each was rejected.",
                ((_DVS6, "Record each design decision and the alternative you rejected"),),
            ),
        ),
    ),
)
