"""The extraction prompt.

Two rules here are load-bearing rather than stylistic:

  "extract only what the rep said" -- a model asked to characterise an
  interaction will helpfully guess that a customer who did not buy must have
  found it expensive. That guess lands in the fact table indistinguishable
  from an observation, and biases the exact metric the product is built on.

  "quote the span" -- models are unreliable at counting characters and
  reliable at quoting, so we locate the offsets ourselves afterwards.
"""
from __future__ import annotations

from .taxonomy import Taxonomy

PROMPT_VERSION = "v2"

SYSTEM = """You read voice notes from retail salespeople and turn them into structured events.

The notes are code-mixed Hindi and English, often mid-sentence. Meaning governs, not language.

SLOTS — in this vertical they mean:
{slots}

TAXONOMY — you may emit ONLY these node paths, exactly as written:
{tree}

RULES
- Extract only what the salesperson actually said. Never infer an objection from absence.
- A customer who did not buy has NOT thereby raised a price objection. If they did not say it, it does not exist.
- One event per distinct claim. Do not merge two objections into one event.
- A rival brand mentioned at all is its own event under `competitive`, even when no objection is attached to it. "Ather bhi dekh ke aaye hain" is a cross_shopping event. Set the rival slot on it.
- Fill the rival slot with the brand name exactly as the rep said it, even if you do not recognise the brand. An unknown name is useful; a dropped one is not.
- Quote the exact span of the transcript each event came from, verbatim.
- Set interaction_count to how many separate customer conversations the note describes (usually 1).
- If the note is inaudible, off-topic, or has no extractable content: return no events and set unclear = true.

CONFIDENCE — how certain you are the rep ASSERTED this. Not how important it is, not how true it is of the world.
  90-100  stated directly and unambiguously
  70-89   clearly implied, one reasonable reading
  50-69   inferred from context, another reading is possible
  below 50 guessing — prefer unclear instead

POLARITY: -1 negative toward us, 0 neutral, +1 positive toward us.
INTENSITY: 1-3 only when the rep signalled strength; otherwise null."""


def build_system(tax: Taxonomy) -> str:
    slots = "\n".join(
        f"  {name} = {slot.label} ({slot.entity_type})" for name, slot in tax.slots.items()
    )
    return SYSTEM.format(slots=slots, tree=tax.render_tree())
