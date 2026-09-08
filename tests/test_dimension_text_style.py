"""Independent geometry checks for position/orientation of complete dimension text."""

import math

import pytest
from build123d import Draft, Edge, Location, Vector

from build123d_drafting import Dimension, SafeDimension, draft_preset


def _ink_at(dimension, point):
    return any(face.is_inside((*point, 0), tolerance=1e-6) for face in dimension.faces())


@pytest.mark.parametrize("orientation", ["aligned", "horizontal"])
@pytest.mark.parametrize("length", [3, 60])
def test_above_has_continuous_actual_ink_and_offset_label(length, orientation):
    draft = draft_preset(text_position="above", text_orientation=orientation)
    dimension = Dimension((0, 0), (length, 0), "above", 8, draft, tolerance=0.1)
    assert "±0.10" in dimension.label
    assert dimension.measured_length == pytest.approx(length)
    assert dimension.label_bbox[1] >= 8 + draft.pad_around_text
    # Probe actual shaft ink through the middle, where inline mode has a gap.
    assert all(_ink_at(dimension, (length * fraction, 8)) for fraction in (0.1, 0.4, 0.5, 0.6, 0.9))
    assert any(
        a[1] == pytest.approx(8)
        and b[1] == pytest.approx(8)
        and min(a[0], b[0]) < length / 2 < max(a[0], b[0])
        for a, b in dimension.segments
    )
    if length == 3:
        assert _ink_at(dimension, (-draft.arrow_length, 8))
        assert _ink_at(dimension, (length + draft.arrow_length, 8))


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("orientation", ["aligned", "horizontal"])
def test_vertical_above_is_left_and_reading_orientation_is_independent(reverse, orientation):
    ends = ((0, 0), (0, 50))
    if reverse:
        ends = ends[::-1]
    draft = draft_preset(text_position="above", text_orientation=orientation)
    dimension = Dimension(*ends, "left", 8, draft, label="50 ±0.1")
    assert dimension.label_bbox[2] <= -8 - draft.pad_around_text
    first, second = dimension.label_polygon[:2]
    if orientation == "aligned":
        assert second[0] == pytest.approx(first[0])
        assert second[1] > first[1]  # Bottom to top reads from the right.
    else:
        assert second[1] == pytest.approx(first[1])
        assert second[0] > first[0]
    assert _ink_at(dimension, (-8, 25))


@pytest.mark.parametrize("angle", [-65, -25, 25, 65, 115, 155])
@pytest.mark.parametrize("orientation", ["aligned", "horizontal"])
def test_oblique_label_region_matches_ink_and_is_endpoint_order_independent(angle, orientation):
    radians = math.radians(angle)
    end = (60 * math.cos(radians), 60 * math.sin(radians))
    draft = draft_preset(text_position="above", text_orientation=orientation)
    forward = Dimension((0, 0), end, "above", 8, draft, label="60 ±0.1")
    reverse = Dimension(end, (0, 0), "above", 8, draft, label="60 ±0.1")
    assert forward.label_bbox == pytest.approx(reverse.label_bbox)
    assert forward.label == reverse.label == "60 ±0.1"
    x0, y0, x1, y1 = forward.label_bbox
    xs, ys = zip(*forward.label_polygon, strict=True)
    assert (x0, y0, x1, y1) == pytest.approx((min(xs), min(ys), max(xs), max(ys)))
    # Label ink exists within the advertised region; no witness or shaft enters it.
    label_faces = [
        face
        for face in forward.faces()
        if x0 - 1e-6 <= face.center().X <= x1 + 1e-6 and y0 - 1e-6 <= face.center().Y <= y1 + 1e-6
    ]
    assert len(label_faces) >= 5
    for face in label_faces:
        box = face.bounding_box()
        assert box.min.X >= x0 - 1e-5 and box.max.X <= x1 + 1e-5
        assert box.min.Y >= y0 - 1e-5 and box.max.Y <= y1 + 1e-5


@pytest.mark.parametrize("position", ["inline", "above"])
@pytest.mark.parametrize("basic", [False, True])
def test_shifted_horizontal_text_does_not_cross_witness_ink(position, basic):
    draft = draft_preset(text_position=position, text_orientation="horizontal")
    # Text at the upper end of a vertical dimension crosses the top witness
    # unless that witness clips against the actual horizontal text region.
    dimension = Dimension(
        (0, 0), (0, 30), "left", 20, draft, label="30 ±0.1", label_offset_x=15, basic=basic
    )
    x0, y0, x1, y1 = dimension.label_bbox
    witnesses = [
        (a, b)
        for a, b in dimension.segments
        if a[1] == pytest.approx(30) and b[1] == pytest.approx(30)
    ]
    assert witnesses
    assert y0 < 30 < y1
    for a, b in witnesses:
        assert max(a[0], b[0]) <= x0 or min(a[0], b[0]) >= x1


@pytest.mark.parametrize("angle", [0, 45, 90])
def test_above_basic_frame_stays_clear_of_dimension_line(angle):
    radians = math.radians(angle)
    u = (math.cos(radians), math.sin(radians))
    dimension = Dimension(
        (0, 0),
        (60 * u[0], 60 * u[1]),
        "above" if angle < 90 else "left",
        8,
        draft_preset(text_position="above"),
        label="60",
        basic=True,
    )
    # The whole basic frame must remain on the reading side of the dimension line.
    n = (-u[1], u[0])
    assert min(x * n[0] + y * n[1] for x, y in dimension.label_polygon) > 8


def test_default_still_has_inline_gap_and_plain_draft_is_supported():
    for draft in (draft_preset(), Draft()):
        dimension = Dimension((0, 0), (60, 0), "above", 8, draft, label="60")
        assert dimension.label_bbox[1] < 8 < dimension.label_bbox[3]
        # Pick a point in the padded gap just outside the text.
        assert not _ink_at(dimension, (dimension.label_bbox[0] - draft.pad_around_text / 2, 8))


def test_inline_basic_frame_excludes_shaft_ink_with_small_padding():
    dimension = Dimension(
        (0, 0),
        (0, 30),
        "left",
        8,
        draft_preset(text_orientation="horizontal", pad_around_text=0.5),
        label="30",
        basic=True,
    )
    x0, y0, x1, y1 = dimension.label_bbox
    assert x0 < -8 < x1
    shafts = [
        (a, b)
        for a, b in dimension.segments
        if a[0] == pytest.approx(-8) and b[0] == pytest.approx(-8)
    ]
    assert len(shafts) == 2
    assert all(max(a[1], b[1]) < y0 or min(a[1], b[1]) > y1 for a, b in shafts)


@pytest.mark.parametrize("basic", [False, True])
@pytest.mark.parametrize("position", ["inline", "above"])
@pytest.mark.parametrize("angle", [0, 60, 90])
def test_zero_padding_keeps_finite_strokes_out_of_text_and_frame(
    monkeypatch, basic, position, angle
):
    import build123d_drafting.helpers as helpers

    line_faces, text_shapes, frames = [], [], []
    rect, ink, trace = helpers._rect_face, helpers._dim_line_ink, helpers.trace

    def record_rect(*args, **kwargs):
        face = rect(*args, **kwargs)
        if face is not None:
            line_faces.append(face)
        return face

    def record_ink(*args, **kwargs):
        result = ink(*args, **kwargs)
        text_shapes.append(result[0][-1])
        return result

    def record_frame(*args, **kwargs):
        frame = trace(*args, **kwargs)
        frames.append(frame)
        return frame

    monkeypatch.setattr(helpers, "_rect_face", record_rect)
    monkeypatch.setattr(helpers, "_dim_line_ink", record_ink)
    monkeypatch.setattr(helpers, "trace", record_frame)
    radians = math.radians(angle)
    dimension = Dimension(
        (0, 0),
        (30 * math.cos(radians), 30 * math.sin(radians)),
        "above" if angle < 90 else "left",
        20,
        draft_preset(text_position=position, text_orientation="horizontal", pad_around_text=0),
        label="88888",
        label_offset_x=15,
        basic=basic,
    )
    assert line_faces and len(text_shapes) == 1
    assert len(frames) == int(basic)
    for text_or_frame in [*text_shapes, *frames]:
        for line in line_faces:
            overlap = line.intersect(text_or_frame)
            assert overlap is None or overlap.area < 1e-8
    if basic:
        bbox = frames[0].bounding_box()
        assert dimension.label_bbox == pytest.approx(
            (bbox.min.X, bbox.min.Y, bbox.max.X, bbox.max.Y), abs=1e-5
        )


@pytest.mark.parametrize("padding", [0, 0.5, 2])
@pytest.mark.parametrize("orientation", ["aligned", "horizontal"])
def test_short_above_label_never_intersects_actual_arrowheads(padding, orientation):
    from build123d_drafting.helpers import _dim_line_ink

    draft = draft_preset(
        text_position="above", text_orientation=orientation, pad_around_text=padding
    )
    faces, _segments, label_geo = _dim_line_ink(
        Vector(0, 0), Vector(3, 0), draft, "888888888888", label_t=1.5
    )
    assert label_geo is not None
    assert label_geo[3] * 2 > 3  # The complete label overhangs both external heads.
    text = faces[-1]
    assert len(text.faces()) == 12
    for other in faces[:-1]:
        overlap = text.intersect(other)
        assert overlap is None or overlap.area < 1e-8


def test_styled_safe_dimension_keeps_long_text_and_transformed_metadata():
    label = "3 ±0.012 COMPLETE"
    dimension = SafeDimension(
        [(0, 0), (3, 0)], label, draft_preset(text_position="above"), fallback_label="~"
    )
    assert dimension.label == label
    assert dimension.measured_length == pytest.approx(3)
    assert dimension.label_bbox[1] > 0
    assert _ink_at(dimension, (1.5, 0))
    moved = dimension.moved(Location(Vector(20, 10, 0), (0, 0, 1), 90))
    for old, new in zip(dimension.label_polygon, moved.label_polygon, strict=True):
        assert new == pytest.approx((20 - old[1], 10 + old[0]))


@pytest.mark.parametrize("style", [{"text_position": "above"}, {"text_orientation": "horizontal"}])
def test_curved_safe_dimension_refuses_unsupported_style(style):
    with pytest.raises(ValueError, match="straight SafeDimension"):
        SafeDimension(
            Edge.make_circle(10, start_angle=0, end_angle=90), "FULL LABEL", draft_preset(**style)
        )


@pytest.mark.parametrize("field", ["text_position", "text_orientation"])
def test_invalid_style_is_refused_before_rendering(field):
    with pytest.raises(ValueError, match=field):
        draft_preset(**{field: "unknown"})
    draft = Draft()
    setattr(draft, field, "unknown")
    with pytest.raises(ValueError, match=field):
        Dimension((0, 0), (60, 0), "above", 8, draft)


def test_styled_safe_dimension_does_not_hide_render_failure(monkeypatch):
    import build123d_drafting.helpers as helpers

    def fail(*args, **kwargs):
        raise ValueError("cannot render complete text")

    monkeypatch.setattr(helpers, "_dim_line_ink", fail)
    with pytest.raises(ValueError, match="complete text"):
        SafeDimension([(0, 0), (30, 0)], "30", draft_preset(text_position="above"))
