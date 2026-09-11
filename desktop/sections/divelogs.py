import toga

from desktop.sections.dive_editor import DiveEditorSection


def build(app) -> toga.Box:
    section = DiveEditorSection(app, service="divelogs", title="Divelogs Dives")
    app.divelogs_section = section
    return section.content
