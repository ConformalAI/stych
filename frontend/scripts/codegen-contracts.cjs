/**
 * Generates src/generated/contracts/*.ts from backend-exported JSON schemas
 * (backend/scripts/export_contract_schemas.py). DO NOT EDIT the output —
 * regenerate via `npm run codegen`. CI fails if these files are stale
 * relative to the schemas.
 */
const { compileFromFile } = require('json-schema-to-typescript');
const fs = require('fs');
const path = require('path');

const SCHEMA_DIR = path.join(__dirname, '..', 'src', 'generated', 'schema');
const OUT_DIR = path.join(__dirname, '..', 'src', 'generated', 'contracts');

function camel(s) {
    return s.replace(/([a-z0-9])([A-Z])/g, '$1$2').replace(/^./, (c) => c.toLowerCase());
}

async function main() {
    // Rebuild the generated directory so removed or renamed schemas cannot
    // leave stale TypeScript modules behind.
    fs.rmSync(OUT_DIR, { recursive: true, force: true });
    fs.mkdirSync(OUT_DIR, { recursive: true });
    const files = fs.readdirSync(SCHEMA_DIR).filter((f) => f.endsWith('.json'));

    for (const file of files) {
        const full = path.join(SCHEMA_DIR, file);
        const parsed = JSON.parse(fs.readFileSync(full, 'utf8'));
        if (parsed.name && Array.isArray(parsed.values)) {
            // Not a JSON Schema — a plain enum-value list. Emit a string union.
            const out = [
                '/** GENERATED FILE — do not edit. Regenerate with `npm run codegen`.',
                ' *  Source of truth: a backend enum (see scripts/export_contract_schemas.py).',
                ' */',
                '',
                `export type ${parsed.name} =`,
                parsed.values.map((v) => `  | '${v}'`).join('\n') + ';',
                '',
            ].join('\n');
            const moduleName = camel(parsed.name) + 's';
            fs.writeFileSync(path.join(OUT_DIR, `${moduleName}.ts`), out);
            console.log(`wrote ${moduleName}.ts (${parsed.values.length} values)`);
            continue;
        }
        if (file === 'viz_literals.json') {
            // Named scalar unions from viz.spec Literals.
            const literals = JSON.parse(fs.readFileSync(full, 'utf8'));
            const lines = [
                '/** GENERATED FILE — do not edit. Regenerate with `npm run codegen`.',
                ' *  Source of truth: backend/viz/spec.py Literal types.',
                ' */',
                '',
            ];
            for (const [name, values] of Object.entries(literals)) {
                lines.push(`export type ${name} = ${values.map((v) => `'${v}'`).join(' | ')};`);
            }
            lines.push('');
            fs.writeFileSync(path.join(OUT_DIR, 'vizLiterals.ts'), lines.join('\n'));
            console.log(`wrote vizLiterals.ts (${Object.keys(literals).length} unions)`);
            continue;
        }
        const ts = await compileFromFile(full, {
            bannerComment:
                '/** GENERATED FILE — do not edit. Regenerate with `npm run codegen`. */',
            additionalProperties: false,
            enableConstEnums: true,
        });
        const base = file.replace(/\.json$/, '');
        // camelCase module name: VizSpecSchema -> vizSpecSchema
        const moduleName = base.charAt(0).toLowerCase() + base.slice(1);
        fs.writeFileSync(path.join(OUT_DIR, `${moduleName}.ts`), ts);
        console.log(`wrote ${moduleName}.ts`);
    }
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
