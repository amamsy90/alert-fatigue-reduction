// Markdown -> DOCX for the course final report.
// node tools/md2docx.js FINAL_REPORT.md submission/report.docx "" figures
// A4, 2.5 cm margins, Times New Roman. LibreOffice may substitute Liberation Serif;
// the submitted PDF should be exported from Word, which has Times New Roman.
// Sizes are half-points: title 32 = 16 pt, heading 1 = 28 = 14 pt, heading 2 = 24 = 12 pt.
// Named styles must be set too. The library default Title is 28 pt and Heading 1/2 are blue.
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell,
  WidthType, ShadingType, BorderStyle, AlignmentType, LevelFormat, ImageRun, Footer,
  PageNumber, TableLayoutType, ExternalHyperlink, PageBreak, UnderlineType,
} = require("docx");

const [, , input, output, , imageDir] = process.argv;
const md = fs.readFileSync(input, "utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
const lines = md.split("\n");

const FONT = "Times New Roman";
const MONO = "Courier New";
const INK = "000000";
const PAGE_W = 11906, PAGE_H = 16838, MARGIN = 1418; // A4, 2.5 cm
const CONTENT_W = PAGE_W - 2 * MARGIN;
const BODY = { before: 0, after: 120, line: 276, lineRule: "auto" };
const SINGLE = { before: 0, after: 0, line: 240, lineRule: "auto" };

function pngSize(data) {
  if (data.length < 24 || data.toString("ascii", 1, 4) !== "PNG") return null;
  return { width: data.readUInt32BE(16), height: data.readUInt32BE(20) };
}

function linkRun(text, url, base) {
  return new ExternalHyperlink({
    link: url,
    children: [new TextRun({
      text, font: base.font || FONT, size: base.size || 24, bold: base.bold,
      color: "0563C1", underline: { type: UnderlineType.SINGLE },
    })],
  });
}

function runs(text, base = {}) {
  const size = base.size || 24;
  const font = base.font || FONT;
  const color = base.color || INK;
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|https?:\/\/[^\s)]+)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(new TextRun({ text: text.slice(last, m.index), font, size, color, bold: base.bold }));
    const t = m[0];
    if (t.startsWith("**")) out.push(new TextRun({ text: t.slice(2, -2), bold: true, font, size, color }));
    else if (t.startsWith("`")) out.push(new TextRun({ text: t.slice(1, -1), font: MONO, size: Math.max(size, 20), color }));
    else if (t.startsWith("[")) {
      const mm = t.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      out.push(linkRun(mm[1], mm[2], { font, size, bold: base.bold }));
    } else out.push(linkRun(t, t, { font, size, bold: base.bold }));
    last = m.index + t.length;
  }
  if (last < text.length) out.push(new TextRun({ text: text.slice(last), font, size, color, bold: base.bold }));
  return out;
}

function bodyParagraph(text, extra = {}) {
  return new Paragraph({
    alignment: AlignmentType.JUSTIFIED,
    spacing: BODY,
    keepNext: extra.keepNext,
    children: runs(text, { size: 24 }),
  });
}

const border = { style: BorderStyle.SINGLE, size: 4, color: "000000" };
function table(rows) {
  const cols = rows[0].length;
  const lens = Array.from({ length: cols }, (_, j) => Math.max(...rows.map(r => (r[j] || "").replace(/\*\*/g, "").length)));
  const wt = lens.map(l => Math.min(Math.max(l, 8), 42));
  const total = wt.reduce((a, b) => a + b, 0);
  const widths = wt.map(l => Math.floor(CONTENT_W * l / total));
  widths[widths.length - 1] += CONTENT_W - widths.reduce((a, b) => a + b, 0);
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: widths,
    layout: TableLayoutType.FIXED,
    rows: rows.map((r, i) => new TableRow({
      tableHeader: i === 0,
      children: widths.map((w, j) => new TableCell({
        width: { size: w, type: WidthType.DXA },
        borders: { top: border, bottom: border, left: border, right: border },
        shading: i === 0 ? { type: ShadingType.CLEAR, fill: "E8EDF2", color: "auto" } : undefined,
        margins: { top: 40, bottom: 40, left: 60, right: 60 },
        children: [new Paragraph({
          spacing: SINGLE,
          children: runs(r[j] || "", { size: 20, bold: i === 0 }),
        })],
      })),
    })),
  });
}

function caption(text) {
  return new Paragraph({
    spacing: { before: 0, after: 60, line: 240, lineRule: "auto" },
    children: [new TextRun({ text, font: FONT, size: 20, color: INK })],
  });
}

const TITLE_RUN = { font: FONT, size: 32, bold: true, color: INK };
const H1_RUN = { font: FONT, size: 28, bold: true, color: INK };
const H2_RUN = { font: FONT, size: 24, bold: true, color: INK };

function heading(text, kind, pageBreakBefore) {
  const main = kind === "main" || kind === "appendix";
  return new Paragraph({
    heading: main ? HeadingLevel.HEADING_1 : HeadingLevel.HEADING_2,
    keepNext: true,
    pageBreakBefore: Boolean(pageBreakBefore),
    spacing: { before: pageBreakBefore ? 0 : 240, after: 120, line: 276, lineRule: "auto" },
    children: [new TextRun({ text, ...(main ? H1_RUN : H2_RUN) })],
  });
}

const children = [];
let para = [];
function flush() {
  if (para.length) {
    children.push(bodyParagraph(para.join(" ")));
    para = [];
  }
}

let titleDone = false;
let expectMembers = false;
let breakNext = false;
for (let i = 0; i < lines.length; i++) {
  const line = lines[i];
  if (/^<!--\s*pagebreak\s*-->\s*$/.test(line)) {
    flush();
    breakNext = true;
    continue;
  }
  if (/^```/.test(line)) {
    flush();
    const lang = line.replace(/^```/, "").trim();
    if (lang === "mermaid") {
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) i++;
      console.error("skipped mermaid block; commit a PNG instead");
      continue;
    }
    const block = [];
    i++;
    while (i < lines.length && !/^```/.test(lines[i])) {
      block.push(lines[i]);
      i++;
    }
    block.forEach((text, idx) => {
      children.push(new Paragraph({
        spacing: { before: 0, after: idx === block.length - 1 ? 120 : 0, line: 240, lineRule: "auto" },
        children: [new TextRun({ text: text || " ", font: MONO, size: 20, color: INK })],
      }));
    });
    continue;
  }
  const h = line.match(/^(#{1,4})\s+(.*)$/);
  if (h) {
    flush();
    const level = h[1].length;
    const text = h[2];
    if (level === 1 && !titleDone) {
      children.push(new Paragraph({
        heading: HeadingLevel.TITLE,
        alignment: AlignmentType.CENTER,
        spacing: { before: 0, after: 60, line: 276, lineRule: "auto" },
        children: [new TextRun({ text, ...TITLE_RUN })],
      }));
      titleDone = true;
      expectMembers = true;
    } else if (level <= 2) {
      children.push(heading(text, "main", breakNext));
      breakNext = false;
    } else {
      children.push(heading(text, "sub", breakNext));
      breakNext = false;
    }
    continue;
  }
  if (/^---+\s*$/.test(line)) { flush(); continue; }
  if (line.trim() === "") { flush(); continue; }
  if (expectMembers && !/^!\[/.test(line) && !/^\|/.test(line)) {
    flush();
    children.push(new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { before: 0, after: 120, line: 276, lineRule: "auto" },
      children: [new TextRun({ text: line.trim(), font: FONT, size: 24, color: INK })],
    }));
    expectMembers = false;
    continue;
  }
  const img = line.match(/^!\[([^\]]*)\]\(([^)]+)\)/);
  if (img) {
    flush();
    const png = path.join(imageDir || ".", path.basename(img[2]).replace(/\.svg$/, ".png"));
    if (fs.existsSync(png)) {
      const data = fs.readFileSync(png);
      const size = pngSize(data) || { width: 1600, height: 900 };
      const maxW = 605;
      const maxH = 360;
      const scale = Math.min(maxW / size.width, maxH / size.height, 1);
      children.push(new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { before: 0, after: 40, line: 240, lineRule: "auto" },
        children: [new ImageRun({
          type: "png",
          data,
          transformation: { width: Math.round(size.width * scale), height: Math.round(size.height * scale) },
        })],
      }));
      if (img[1]) children.push(caption(img[1]));
    }
    continue;
  }
  if (/^(Table|Figure)\s+\d+\./.test(line.trim()) && i + 1 < lines.length && /^\|/.test(lines[i + 1])) {
    flush();
    children.push(caption(line.trim()));
    continue;
  }
  if (/^\|/.test(line)) {
    flush();
    const rows = [];
    while (i < lines.length && /^\|/.test(lines[i])) {
      const cells = lines[i].trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      if (!cells.every(c => /^:?-{2,}:?$/.test(c))) rows.push(cells);
      i++;
    }
    i--;
    if (rows.length) children.push(table(rows));
    continue;
  }
  const bullet = line.match(/^(\s*)[-*]\s+(.*)$/);
  const num = line.match(/^(\s*)\d+\.\s+(.*)$/);
  if (bullet || num) {
    flush();
    const m = bullet || num;
    let text = m[2];
    while (i + 1 < lines.length && /^\s{2,}\S/.test(lines[i + 1]) && !/^\s*([-*]|\d+\.)\s/.test(lines[i + 1])) {
      text += " " + lines[++i].trim();
    }
    children.push(new Paragraph({
      alignment: AlignmentType.JUSTIFIED,
      numbering: { reference: bullet ? "bullets" : "numbers", level: m[1].length >= 2 ? 1 : 0 },
      spacing: BODY,
      children: runs(text, { size: 24 }),
    }));
    continue;
  }
  if (/^>\s?/.test(line)) {
    para.push(line.replace(/^>\s?/, "").trim());
    continue;
  }
  para.push(line.trim());
}
flush();

const doc = new Document({
  creator: "Aishah Mohamed Alyammahi",
  styles: {
    default: {
      document: { run: { font: FONT, size: 24, color: INK } },
      title: { run: TITLE_RUN },
      heading1: { run: H1_RUN },
      heading2: { run: H2_RUN },
      heading3: { run: H2_RUN },
      heading4: { run: H2_RUN },
      heading5: { run: H2_RUN },
      heading6: { run: H2_RUN },
    },
    characterStyles: [{
      id: "Hyperlink",
      name: "Hyperlink",
      run: { color: "0563C1", underline: { type: UnderlineType.SINGLE } },
    }],
  },
  numbering: {
    config: [
      { reference: "bullets", levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 420, hanging: 240 } } } },
        { level: 1, format: LevelFormat.BULLET, text: "–", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 240 } } } },
      ] },
      { reference: "numbers", levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 420, hanging: 280 } } } },
        { level: 1, format: LevelFormat.LOWER_LETTER, text: "%2)", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 280 } } } },
      ] },
    ],
  },
  sections: [{
    properties: { page: { size: { width: PAGE_W, height: PAGE_H }, margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN } } },
    footers: { default: new Footer({ children: [new Paragraph({
      alignment: AlignmentType.RIGHT,
      children: [new TextRun({ children: [PageNumber.CURRENT], size: 20, font: FONT, color: INK })],
    })] }) },
    children,
  }],
});
Packer.toBuffer(doc).then(buf => { fs.writeFileSync(output, buf); console.log("wrote", output); });
