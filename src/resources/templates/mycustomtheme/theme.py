"""
RenderCV Custom Theme Configuration

Defines ThemeOptions for the custom LaTeX theme used by JobForge.
The user will customize the .j2.tex templates to match their Overleaf design.
"""

from typing import Optional

from pydantic import BaseModel, Field


class ThemeOptions(BaseModel):
    """
    Configuration options for the custom RenderCV theme.
    These values are accessible in Jinja2 templates via <<theme.*>>.
    """

    # Font settings
    primary_color: str = Field(
        default="rgb(0,79,144)",
        description="Primary accent color in LaTeX rgb format",
    )
    font_family: str = Field(
        default="Latin Modern",
        description="Main font family for the resume",
    )
    font_size: str = Field(
        default="10pt",
        description="Base font size",
    )

    # Margins
    page_top_margin: str = Field(default="1.5cm")
    page_bottom_margin: str = Field(default="1.5cm")
    page_left_margin: str = Field(default="1.5cm")
    page_right_margin: str = Field(default="1.5cm")

    # Section styling
    section_title_font_size: str = Field(default="1.1em")
    section_title_bold: bool = Field(default=True)
    section_title_underline: bool = Field(default=True)

    # Entry styling
    entry_date_width: str = Field(default="3.5cm")
    entry_spacing: str = Field(default="0.4em")

    # Header
    show_phone: bool = Field(default=True)
    show_email: bool = Field(default=True)
    show_website: bool = Field(default=True)
    show_location: bool = Field(default=True)

    # Page
    paper_size: str = Field(default="letterpaper")
