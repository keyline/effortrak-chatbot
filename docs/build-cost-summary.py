"""Build the manager PDF and editable DOCX from Markdown (requires reportlab)."""
from html import escape
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle


source = Path(__file__).with_name('ai-chatbot-cost-summary.md')
styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name='CostBody', fontName='Helvetica', fontSize=9, leading=12, spaceAfter=6))
styles.add(ParagraphStyle(name='CostCell', fontName='Helvetica', fontSize=8, leading=11))
styles['Title'].fontSize = 18
styles['Title'].leading = 22
styles['Heading2'].fontSize = 11
styles['Heading2'].leading = 14
lines = source.read_text(encoding='utf-8').splitlines()
story = []
blocks = []
i = 0
while i < len(lines):
    line = lines[i]
    i += 1
    if not line.strip():
        continue
    if line.startswith('|'):
        rows = [line]
        while i < len(lines) and lines[i].startswith('|'):
            rows.append(lines[i])
            i += 1
        data = []
        word_rows = []
        for row in rows:
            cells = [cell.strip() for cell in row.strip('|').split('|')]
            if all(re.fullmatch(r':?-+:?', cell) for cell in cells):
                continue
            data.append([Paragraph(escape(cell), styles['CostCell']) for cell in cells])
            word_rows.append(cells)
        blocks.append(('table', word_rows))
        widths = [165, 334] if len(data[0]) == 2 else [499 / len(data[0])] * len(data[0])
        table = Table(data, colWidths=widths, repeatRows=1, hAlign='LEFT')
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e8f0f1')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7f9fa')]),
            ('LINEBELOW', (0, 0), (-1, -1), 0.3, colors.HexColor('#cdd8da')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        story.extend([table, Spacer(1, 8)])
        continue
    style = styles['CostBody']
    kind = 'Normal'
    if line.startswith('# '):
        line, style = line[2:], styles['Title']
        kind = 'Title'
    elif line.startswith('## '):
        line, style = line[3:], styles['Heading2']
        kind = 'Heading1'
    blocks.append((kind, line))
    content = escape(line)
    content = re.sub(r'(https://[^\s<]+)', r'<link href="\1" color="#126454">\1</link>', content)
    story.append(Paragraph(content, style))


def footer(canvas, doc):
    canvas.setFont('Helvetica', 8)
    canvas.setFillColor(colors.HexColor('#666666'))
    canvas.drawString(48, 24, 'Effortrak | Simple Cost Guide | 27 October 2026')
    canvas.drawRightString(A4[0] - 48, 24, str(doc.page))


SimpleDocTemplate(str(source.with_suffix('.pdf')), pagesize=A4,
                  title='Effortrak Chatbot: Simple Cost Guide', author='Effortrak',
                  leftMargin=48, rightMargin=48, topMargin=36, bottomMargin=42).build(
                      story, onFirstPage=footer, onLaterPages=footer)
print('Updated cost summary PDF.')

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
ET.register_namespace('w', W)


def element(parent, name, **attrs):
    return ET.SubElement(parent, f'{{{W}}}{name}',
                         {f'{{{W}}}{key}': str(value) for key, value in attrs.items()})


def paragraph(parent, text, style='Normal', bold=False):
    p = element(parent, 'p')
    element(element(p, 'pPr'), 'pStyle', val=style)
    run = element(p, 'r')
    if bold:
        element(element(run, 'rPr'), 'b')
    element(run, 't').text = text


document = ET.Element(f'{{{W}}}document')
body = element(document, 'body')
for kind, value in blocks:
    if kind != 'table':
        paragraph(body, value, kind)
        continue
    table = element(body, 'tbl')
    props = element(table, 'tblPr')
    element(props, 'tblW', w=9900, type='dxa')
    element(props, 'tblLayout', type='fixed')
    borders = element(props, 'tblBorders')
    for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        element(borders, edge, val='single', sz=4, color='CDD8DA')
    widths = [3300, 6600] if len(value[0]) == 2 else [9900 // len(value[0])] * len(value[0])
    grid = element(table, 'tblGrid')
    for width in widths:
        element(grid, 'gridCol', w=width)
    for index, row in enumerate(value):
        tr = element(table, 'tr')
        tr_props = element(tr, 'trPr')
        element(tr_props, 'cantSplit')
        if index == 0:
            element(tr_props, 'tblHeader')
        for width, text in zip(widths, row):
            cell = element(tr, 'tc')
            cp = element(cell, 'tcPr')
            element(cp, 'tcW', w=width, type='dxa')
            if index == 0:
                element(cp, 'shd', fill='E8F0F1')
            paragraph(cell, text, bold=index == 0)
    paragraph(body, '')
section = element(body, 'sectPr')
element(section, 'pgSz', w=11906, h=16838)
element(section, 'pgMar', top=720, right=1003, bottom=840, left=1003, header=360, footer=360, gutter=0)

styles_xml = ET.Element(f'{{{W}}}styles')
defaults = element(styles_xml, 'docDefaults')
rp = element(element(defaults, 'rPrDefault'), 'rPr')
element(rp, 'rFonts', ascii='Arial', hAnsi='Arial')
element(rp, 'sz', val=20)
element(element(element(defaults, 'pPrDefault'), 'pPr'), 'spacing', after=120, line=276, lineRule='auto')
for name, size in [('Normal', 20), ('Title', 36), ('Heading1', 24)]:
    style = element(styles_xml, 'style', type='paragraph', styleId=name)
    element(style, 'name', val=name)
    if name != 'Normal':
        element(style, 'basedOn', val='Normal')
        element(element(style, 'pPr'), 'keepNext')
    rp = element(style, 'rPr')
    element(rp, 'sz', val=size)
    if name != 'Normal':
        element(rp, 'b')

types_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
types = ET.Element(f'{{{types_ns}}}Types')
ET.SubElement(types, f'{{{types_ns}}}Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
ET.SubElement(types, f'{{{types_ns}}}Default', Extension='xml', ContentType='application/xml')
for part, content_type in [('document', 'document.main'), ('styles', 'styles')]:
    ET.SubElement(types, f'{{{types_ns}}}Override', PartName=f'/word/{part}.xml',
                  ContentType=f'application/vnd.openxmlformats-officedocument.wordprocessingml.{content_type}+xml')


def relationships(target, relation):
    ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
    root = ET.Element(f'{{{ns}}}Relationships')
    ET.SubElement(root, f'{{{ns}}}Relationship', Id='rId1', Target=target,
                  Type=f'http://schemas.openxmlformats.org/officeDocument/2006/relationships/{relation}')
    return root


word_target = source.with_suffix('.docx')
try:
    word_package = ZipFile(word_target, 'w', ZIP_DEFLATED)
except PermissionError:
    word_target = source.with_name(source.stem + '-updated.docx')
    word_package = ZipFile(word_target, 'w', ZIP_DEFLATED)
with word_package as package:
    for name, root in [('[Content_Types].xml', types),
                       ('_rels/.rels', relationships('word/document.xml', 'officeDocument')),
                       ('word/_rels/document.xml.rels', relationships('styles.xml', 'styles')),
                       ('word/document.xml', document), ('word/styles.xml', styles_xml)]:
        package.writestr(name, ET.tostring(root, encoding='utf-8', xml_declaration=True))
with ZipFile(word_target) as package:
    assert package.testzip() is None
    for name in package.namelist():
        ET.fromstring(package.read(name))
    root = ET.fromstring(package.read('word/document.xml'))
    assert len(root.findall(f'.//{{{W}}}tbl')) == sum(kind == 'table' for kind, _ in blocks)
    assert root.find(f'.//{{{W}}}t').text == blocks[0][1]
print(f'Created and validated editable Word document: {word_target}')
