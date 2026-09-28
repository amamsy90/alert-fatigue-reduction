// Minimal Markdown -> DOCX for this project's docs. Needs Node + `npm install docx`.
// Regenerate the Word files: node tools/md2docx.js FINAL_REPORT.md submission/Alert_Fatigue_Final_Report.docx "" <folder with figure PNGs>
// usage: node md2docx.js input.md output.docx "Subtitle line" [imageDir]
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell,
  WidthType, ShadingType, BorderStyle, AlignmentType, LevelFormat, ImageRun, Footer,
  PageNumber, TableLayoutType,
} = require("docx");

const [, , input, output, subtitle, imageDir] = process.argv;
const md = fs.readFileSync(input, "utf8").replace(/\r\n/g, "\n");
const lines = md.split("\n");

const FONT = "Calibri";
const INK = "1F1F1E";
const MUTED = "5C5B55";
const ACCENT = "1C3A5C";
const PAGE_W = 11906, MARGIN = 1134; // A4, 2 cm margins
const CONTENT_W = PAGE_W - 2 * MARGIN;

function runs(text, base = {}) {
  // **bold**, *italic*, `code`, [text](url) -> text
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1");
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(new TextRun({ text: text.slice(last, m.index), font: FONT, color: INK, ...base }));
    const t = m[0];
    if (t.startsWith("**")) out.push(new TextRun({ text: t.slice(2, -2), bold: true, font: FONT, color: INK, ...base }));
    else if (t.startsWith("`")) out.push(new TextRun({ text: t.slice(1, -1), font: "Consolas", size: 19, color: "3A3A38", ...base }));
    else out.push(new TextRun({ text: t.slice(1, -1), italics: true, font: FONT, color: INK, ...base }));
    last = m.index + t.length;
  }
  if (last < text.length) out.push(new TextRun({ text: text.slice(last), font: FONT, color: INK, ...base }));
  return out;
}

const border = { style: BorderStyle.SINGLE, size: 4, color: "D7DDE3" };
function table(rows) {
  const cols = rows[0].length;
  // width by content length, min share
  const lens = Array.from({ length: cols }, (_, j) => Math.max(...rows.map(r => (r[j] || "").replace(/\*\*/g, "").length)));
  const wt = lens.map(l => Math.min(Math.max(l, 9), 48));
  const total = wt.reduce((a, b) => a + b, 0);
  let widths = wt.map(l => Math.floor(CONTENT_W * l / total));
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
        margins: { top: 60, bottom: 60, left: 90, right: 90 },
        children: [new Paragraph({ children: runs(r[j] || "", { size: 18, bold: i === 0 ? true : undefined }) })],
      })),
    })),
  });
}

const children = [];
let para = [];
function flush() {
  if (para.length) {
    children.push(new Paragraph({ spacing: { after: 120, line: 276 }, children: runs(para.join(" "), { size: 21 }) }));
    para = [];
  }
}

let first = true;
for (let i = 0; i < lines.length; i++) {
  const line = lines[i];
  if (/^```/.test(line)) {
    flush();
    i++;
    while (i < lines.length && !/^```/.test(lines[i])) {
      children.push(new Paragraph({
        shading: { type: ShadingType.CLEAR, fill: "F4F3EF", color: "auto" },
        spacing: { after: 0 },
        children: [new TextRun({ text: lines[i] || " ", font: "Consolas", size: 18, color: "2B2B2A" })],
      }));
      i++;
    }
    children.push(new Paragraph({ spacing: { after: 120 }, children: [] }));
    continue;
  }
  const h = line.match(/^(#{1,3})\s+(.*)$/);
  if (h) {
    flush();
    const level = h[1].length;
    if (level === 1 && first) {
      children.push(new Paragraph({ heading: HeadingLevel.TITLE, spacing: { after: 120 },
        children: [new TextRun({ text: h[2], font: FONT, size: 40, bold: true, color: ACCENT })] }));
      if (subtitle) children.push(new Paragraph({ spacing: { after: 240 },
        children: [new TextRun({ text: subtitle, font: FONT, size: 20, color: MUTED })] }));
      first = false;
    } else {
      children.push(new Paragraph({
        heading: level <= 2 ? HeadingLevel.HEADING_1 : HeadingLevel.HEADING_2,
        spacing: { before: level <= 2 ? 280 : 200, after: 100 },
        children: [new TextRun({ text: h[2], font: FONT, size: level <= 2 ? 28 : 24, bold: true, color: ACCENT })],
      }));
    }
    continue;
  }
  if (/^---+\s*$/.test(line)) { flush(); continue; }
  const img = line.match(/^!\[([^\]]*)\]\(([^)]+)\)/);
  if (img) {
    flush();
    const png = path.join(imageDir || ".", path.basename(img[2]).replace(/\.svg$/, ".png"));
    if (fs.existsSync(png)) {
      children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 120, after: 60 },
        children: [new ImageRun({ type: "png", data: fs.readFileSync(png), transformation: { width: 600, height: 318 } })] }));
      children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 },
        children: [new TextRun({ text: img[1], italics: true, size: 18, font: FONT, color: MUTED })] }));
    }
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
    children.push(table(rows));
    children.push(new Paragraph({ spacing: { after: 120 }, children: [] }));
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
      numbering: { reference: bullet ? "bullets" : "numbers", level: m[1].length >= 2 ? 1 : 0 },
      spacing: { after: 60, line: 264 },
      children: runs(text, { size: 21 }),
    }));
    continue;
  }
  if (/^>\s?/.test(line)) {
    flush();
    children.push(new Paragraph({ spacing: { after: 120 }, indent: { left: 360 },
      border: { left: { style: BorderStyle.SINGLE, size: 12, color: "B0894B", space: 8 } },
      children: runs(line.replace(/^>\s?/, ""), { size: 20, color: MUTED }) }));
    continue;
  }
  if (line.trim() === "") { flush(); continue; }
  para.push(line.trim());
}
flush();

const doc = new Document({
  creator: "Alert Fatigue project",
  styles: { default: { document: { run: { font: FONT, size: 21 } } } },
  numbering: {
    config: [
      { reference: "bullets", levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 270 } } } },
        { level: 1, format: LevelFormat.BULLET, text: "–", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 1000, hanging: 270 } } } },
      ] },
      { reference: "numbers", levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 300 } } } },
        { level: 1, format: LevelFormat.LOWER_LETTER, text: "%2)", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 1000, hanging: 300 } } } },
      ] },
    ],
  },
  sections: [{
    properties: { page: { size: { width: PAGE_W, height: 16838 }, margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN } } },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER,
      children: [new TextRun({ children: [PageNumber.CURRENT], size: 18, color: MUTED, font: FONT })] })] }) },
    children,
  }],
});
Packer.toBuffer(doc).then(buf => { fs.writeFileSync(output, buf); console.log("wrote", output); });
