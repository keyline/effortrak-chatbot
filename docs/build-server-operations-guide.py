"""Create an editable Word version of the server guide using the standard library."""
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED


source = Path(__file__).with_name('server-operations-guide.md')
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
ET.register_namespace('w', W)


def element(parent, name, **attrs):
    return ET.SubElement(parent, f'{{{W}}}{name}',
                         {f'{{{W}}}{key}': str(value) for key, value in attrs.items()})


document = ET.Element(f'{{{W}}}document')
body = element(document, 'body')
in_code = False
code_lines = []
pending = []


def paragraph(text, style='Normal'):
    p = element(body, 'p')
    element(element(p, 'pPr'), 'pStyle', val=style)
    for token in re.split(r'(\*\*[^*]+\*\*|`[^`]+`)', text):
        if not token:
            continue
        run = element(p, 'r')
        if token.startswith('**') and token.endswith('**'):
            element(element(run, 'rPr'), 'b')
            token = token[2:-2]
        elif token.startswith('`') and token.endswith('`'):
            rp = element(run, 'rPr')
            element(rp, 'rFonts', ascii='Consolas', hAnsi='Consolas')
            token = token[1:-1]
        text_element = element(run, 't')
        text_element.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        text_element.text = token


def flush():
    if pending:
        paragraph(' '.join(pending))
        pending.clear()


for line in source.read_text(encoding='utf-8').splitlines():
    if line.startswith('```'):
        flush()
        in_code = not in_code
        continue
    if in_code:
        # Literal runs preserve shell punctuation, indentation and backslashes.
        p = element(body, 'p')
        element(element(p, 'pPr'), 'pStyle', val='Code')
        text_element = element(element(p, 'r'), 't')
        text_element.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        text_element.text = line
        code_lines.append(line)
    elif not line.strip():
        flush()
    elif line.startswith('# '):
        flush()
        paragraph(line[2:], 'Title')
    elif line.startswith('## '):
        flush()
        paragraph(line[3:], 'Heading1')
    elif line.startswith('- ') or re.match(r'^\d+\. ', line):
        flush()
        pending.append(line)
    else:
        pending.append(line.strip())
flush()
assert not in_code, 'Unclosed Markdown code block'
section = element(body, 'sectPr')
element(section, 'pgSz', w=11906, h=16838)
element(section, 'pgMar', top=850, right=850, bottom=850, left=850, header=360, footer=360, gutter=0)

styles = ET.Element(f'{{{W}}}styles')
for name, font, size in [('Normal', 'Arial', 20), ('Title', 'Arial', 34),
                         ('Heading1', 'Arial', 24), ('Code', 'Consolas', 17)]:
    style = element(styles, 'style', type='paragraph', styleId=name)
    element(style, 'name', val=name)
    if name == 'Normal':
        style.set(f'{{{W}}}default', '1')
    else:
        element(style, 'basedOn', val='Normal')
    pp = element(style, 'pPr')
    element(pp, 'spacing', after=0 if name == 'Code' else 120,
            line=240 if name == 'Code' else 276, lineRule='auto')
    if name in ('Title', 'Heading1'):
        element(pp, 'keepNext')
    if name == 'Code':
        element(pp, 'shd', fill='F1F4F5')
    rp = element(style, 'rPr')
    element(rp, 'rFonts', ascii=font, hAnsi=font)
    element(rp, 'sz', val=size)
    if name in ('Title', 'Heading1'):
        element(rp, 'b')

types_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
types = ET.Element(f'{{{types_ns}}}Types')
for extension, content_type in [('rels', 'application/vnd.openxmlformats-package.relationships+xml'),
                                 ('xml', 'application/xml')]:
    ET.SubElement(types, f'{{{types_ns}}}Default', Extension=extension, ContentType=content_type)
for part, suffix in [('document', 'document.main'), ('styles', 'styles')]:
    ET.SubElement(types, f'{{{types_ns}}}Override', PartName=f'/word/{part}.xml',
                  ContentType=f'application/vnd.openxmlformats-officedocument.wordprocessingml.{suffix}+xml')


def relationships(target, relation):
    ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
    root = ET.Element(f'{{{ns}}}Relationships')
    ET.SubElement(root, f'{{{ns}}}Relationship', Id='rId1', Target=target,
                  Type=f'http://schemas.openxmlformats.org/officeDocument/2006/relationships/{relation}')
    return root


target = source.with_suffix('.docx')
try:
    package = ZipFile(target, 'w', ZIP_DEFLATED)
except PermissionError:
    target = source.with_name(source.stem + '-updated.docx')
    package = ZipFile(target, 'w', ZIP_DEFLATED)
with package:
    for name, root in [('[Content_Types].xml', types),
                       ('_rels/.rels', relationships('word/document.xml', 'officeDocument')),
                       ('word/_rels/document.xml.rels', relationships('styles.xml', 'styles')),
                       ('word/document.xml', document), ('word/styles.xml', styles)]:
        package.writestr(name, ET.tostring(root, encoding='utf-8', xml_declaration=True))

with ZipFile(target) as package:
    assert package.testzip() is None
    for name in package.namelist():
        ET.fromstring(package.read(name))
    root = ET.fromstring(package.read('word/document.xml'))
    exported_commands = []
    for p in root.findall(f'.//{{{W}}}p'):
        style = p.find(f'{{{W}}}pPr/{{{W}}}pStyle')
        if style is not None and style.get(f'{{{W}}}val') == 'Code':
            exported_commands.append(''.join(t.text or '' for t in p.findall(f'.//{{{W}}}t')))
    assert exported_commands == code_lines, 'Exported shell commands changed'
print(f'Created {target}; validated all {len(code_lines)} command lines unchanged.')
