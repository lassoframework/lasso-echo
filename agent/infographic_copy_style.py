"""Typography rules for rendered LASSO copy, without altering stored Brain sources."""
import re


def display_text(value):
    text = str(value or "")
    # Display the destination without its protocol while preserving its address.
    text = re.sub(r"https?://", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d):(?=\d)", ".", text)
    return text.replace(";", ",").replace(":", " — ")
