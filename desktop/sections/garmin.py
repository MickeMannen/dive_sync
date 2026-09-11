import toga

from desktop.sections.dive_editor import DiveEditorSection


def build(app) -> toga.Box:
    section = DiveEditorSection(app, service="garmin", title="Garmin Dives")
    app.garmin_section = section
    return section.content
