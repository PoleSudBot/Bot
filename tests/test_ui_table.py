from pathlib import Path

from jinja2 import Environment, FileSystemLoader
import nonebot

nonebot.init()

from zhenxun.ui.models.core.content import TableData


def test_table_divider_marks_only_the_next_row():
    table = TableData(title="测试").add_row(["第一行"])

    result = table.add_divider().add_row(["第二行"]).add_row(["第三行"])

    assert result is table
    assert table.divider_before_rows == {1}
    assert [row[0].content for row in table.rows] == ["第一行", "第二行", "第三行"]
    assert table.get_render_data()["divider_before_rows"] == {1}


def test_table_without_divider_keeps_default_render_state():
    table = TableData(title="测试").add_row(["普通行"])

    assert table.divider_before_rows == set()


def test_default_table_template_applies_row_divider_class():
    resources = Path(__file__).parents[1] / "resources" / "themes" / "default"
    template_source = (resources / "components/core/table/main.html").read_text()
    style = (resources / "components/core/table/style.css").read_text()
    table = (
        TableData(title="测试")
        .add_row(["第一行"])
        .add_divider()
        .add_row(["查询者"])
    )
    rendered = Environment(loader=FileSystemLoader(resources)).get_template(
        "components/core/table/main.html"
    ).render(data=table.get_render_data())

    assert "loop.index0 in data.divider_before_rows" in template_source
    assert rendered.count('class="row-divider"') == 1
    assert rendered.index("查询者") > rendered.index('class="row-divider"')
    assert "tbody tr.row-divider td" in style
