import { useEffect, useRef } from 'react';

/**
 * FoldLoader — a minimal, modernist loading icon.
 *
 * One continuous line perpetually folds through the three primary forms
 * (triangle -> square -> circle -> triangle) while a single accent dot rides
 * along it. No dependencies. Pure canvas.
 *
 * `color`/`accent` accept a concrete color, "currentColor" (inherits the
 * surrounding CSS text color), or a `var(--token)` reference — the latter two
 * are resolved against the live computed style so the icon tracks the site's
 * light/dark palette. Pass `accent={null}` for a fully monochrome look.
 */
interface FoldLoaderProps {
    size?: number;
    color?: string;
    accent?: string | null;
    speed?: number;
}

export default function FoldLoader({
    size = 48,
    color = 'currentColor',
    accent = 'var(--c-signal)',
    speed = 1,
}: FoldLoaderProps) {
    const ref = useRef<HTMLCanvasElement>(null);

    useEffect(() => {
        const cv = ref.current;
        if (!cv) return;
        const ctx = cv.getContext('2d');
        if (!ctx) return;
        const dpr = window.devicePixelRatio || 1;
        cv.width = size * dpr;
        cv.height = size * dpr;
        ctx.scale(dpr, dpr);

        const M = 256; // sample points around each shape's perimeter

        // Sample a closed polygon into M evenly-spaced points (by arc length).
        const polyPts = (verts: number[][]) => {
            let total = 0;
            const segs: { a: number[]; b: number[]; len: number }[] = [];
            for (let i = 0; i < verts.length; i++) {
                const a = verts[i];
                const b = verts[(i + 1) % verts.length];
                const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
                segs.push({ a, b, len });
                total += len;
            }
            const out: number[][] = [];
            for (let i = 0; i < M; i++) {
                let d = (i / M) * total;
                for (const s of segs) {
                    if (d <= s.len) {
                        const t = s.len ? d / s.len : 0;
                        out.push([s.a[0] + (s.b[0] - s.a[0]) * t, s.a[1] + (s.b[1] - s.a[1]) * t]);
                        break;
                    }
                    d -= s.len;
                }
            }
            return out;
        };

        // Three primary forms, unit-scaled, each starting from top-center clockwise
        // so the point correspondence produces a clean fold.
        const R = 1;
        const circle: number[][] = [];
        for (let i = 0; i < M; i++) {
            const a = -Math.PI / 2 + (i / M) * Math.PI * 2;
            circle.push([Math.cos(a) * R, Math.sin(a) * R]);
        }
        const sq = R * 0.82;
        const square = polyPts([
            [0, -sq],
            [sq, -sq],
            [sq, sq],
            [-sq, sq],
            [-sq, -sq],
        ]);
        const rt = R * 1.02;
        const triangle = polyPts([
            [0, -rt],
            [Math.cos(Math.PI / 6) * rt, Math.sin(Math.PI / 6) * rt],
            [-Math.cos(Math.PI / 6) * rt, Math.sin(Math.PI / 6) * rt],
        ]);
        const shapes = [triangle, square, circle];

        const ease = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

        // Resolve "currentColor" / "var(--token)" against the live computed style
        // so the icon matches the active (light/dark) palette. Resolved once at
        // mount — the loader is transient, so a theme flip mid-spin is a non-issue.
        const resolve = (c: string | null): string | null => {
            if (!c) return c;
            const cs = getComputedStyle(cv);
            if (c === 'currentColor') return cs.color || '#111';
            const m = /^var\((--[\w-]+)\)$/.exec(c.trim());
            if (m) return cs.getPropertyValue(m[1]).trim() || cs.color || '#111';
            return c;
        };
        const strokeColor = resolve(color) as string;
        const accentColor = resolve(accent);

        const cx = size / 2;
        const cy = size / 2;
        const rad = size * 0.34;
        const lineWidth = Math.max(1.2, size * 0.025);
        const dotR = Math.max(1.6, size * 0.045);
        const PERIOD = 3600 / speed;

        let raf = 0;
        const draw = (t: number) => {
            ctx.clearRect(0, 0, size, size);

            const phase = ((t % PERIOD) / PERIOD) * 3;
            const idx = Math.floor(phase);
            const local = ease(phase - idx);
            const A = shapes[idx % 3];
            const B = shapes[(idx + 1) % 3];

            const rot = (t / PERIOD) * 0.55;
            const cos = Math.cos(rot);
            const sin = Math.sin(rot);

            const pts: number[][] = [];
            for (let i = 0; i < M; i++) {
                const x = A[i][0] + (B[i][0] - A[i][0]) * local;
                const y = A[i][1] + (B[i][1] - A[i][1]) * local;
                const rx = x * cos - y * sin;
                const ry = x * sin + y * cos;
                pts.push([cx + rx * rad, cy + ry * rad]);
            }

            ctx.lineWidth = lineWidth;
            ctx.lineJoin = 'round';
            ctx.lineCap = 'round';
            ctx.strokeStyle = strokeColor;
            ctx.beginPath();
            ctx.moveTo(pts[0][0], pts[0][1]);
            for (let i = 1; i < M; i++) ctx.lineTo(pts[i][0], pts[i][1]);
            ctx.closePath();
            ctx.stroke();

            if (accentColor) {
                const di = Math.floor(((t / 1400) % 1) * M);
                const dp = pts[di];
                ctx.beginPath();
                ctx.arc(dp[0], dp[1], dotR, 0, Math.PI * 2);
                ctx.fillStyle = accentColor;
                ctx.fill();
            }

            raf = requestAnimationFrame(draw);
        };
        raf = requestAnimationFrame(draw);

        return () => cancelAnimationFrame(raf);
    }, [size, color, accent, speed]);

    return (
        <canvas
            ref={ref}
            role="img"
            aria-label="Loading"
            style={{ width: size, height: size, display: 'block' }}
        />
    );
}
