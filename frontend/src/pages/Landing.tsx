import { useState, useRef, useCallback, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Paperclip, X, FileText, ArrowUp, Sun, Moon, Bot } from 'lucide-react';
import { useAuthStore } from '../store/useAuthStore';
import { useStore } from '../store/useStore';
import { googleSignIn } from '../services/auth';
import { uploadFile } from '../services/api';

/* ─────────────────────────────────────────────
   Prompt suggestions that cycle in the input
   ───────────────────────────────────────────── */
const PROMPTS = [
    'Clean my dataset and remove duplicates…',
    'What trends are hiding in my sales data?',
    'Find and fix missing values in my CSV…',
    'Show me a statistical summary of each column…',
    'Which columns have the most outliers?',
    'Compare performance metrics across categories…',
    'Detect anomalies in my time-series data…',
    'Generate a correlation matrix for my features…',
];

/* ─────────────────────────────────────────────
   Interactive Canvas Grid Background (dark mode)
   ───────────────────────────────────────────── */
function GridBackground() {
    const canvasRef = useRef<HTMLCanvasElement>(null);

    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        const ctx = canvas.getContext('2d', { alpha: false });
        if (!ctx) return;

        const CELL = 60;
        const DECAY_K = 2.3;
        const MIN_B = 0.004;
        const HL_R = 15, HL_G = 185, HL_B = 129;
        const HL_PEAK_A = 0.14;

        const active = new Map<number, number>();
        let cols = 0, rows = 0;
        let mouseX = -9999, mouseY = -9999;
        let animId = 0, lastTime = 0, dpr = 1;

        const off = document.createElement('canvas');
        const oCtx = off.getContext('2d')!;

        const paintBase = () => {
            const w = window.innerWidth, h = window.innerHeight;
            off.width = w * dpr; off.height = h * dpr;
            oCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const rad = (145 * Math.PI) / 180;
            const dx = Math.sin(rad), dy = -Math.cos(rad);
            const halfLen = (Math.abs(w * dx) + Math.abs(h * dy)) / 2;
            const cx = w / 2, cy = h / 2;
            const grad = oCtx.createLinearGradient(cx - dx * halfLen, cy - dy * halfLen, cx + dx * halfLen, cy + dy * halfLen);
            grad.addColorStop(0, '#08090d');
            grad.addColorStop(0.25, '#10121a');
            grad.addColorStop(0.5, '#181b27');
            grad.addColorStop(0.75, '#1a1d2e');
            grad.addColorStop(1, '#0e1020');
            oCtx.fillStyle = grad;
            oCtx.fillRect(0, 0, w, h);
        };

        const resize = () => {
            dpr = window.devicePixelRatio || 1;
            const w = window.innerWidth, h = window.innerHeight;
            canvas.width = w * dpr; canvas.height = h * dpr;
            canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
            cols = Math.ceil(w / CELL) + 1;
            rows = Math.ceil(h / CELL) + 1;
            paintBase(); active.clear();
        };

        resize();

        const onMouseMove = (e: MouseEvent) => { mouseX = e.clientX; mouseY = e.clientY; };
        const onMouseLeave = () => { mouseX = -9999; mouseY = -9999; };

        const loop = (time: number) => {
            const dt = lastTime ? Math.min((time - lastTime) / 1000, 0.1) : 0.016;
            lastTime = time;
            const decay = Math.exp(-DECAY_K * dt);
            for (const [idx, b] of active) {
                const next = b * decay;
                if (next < MIN_B) active.delete(idx); else active.set(idx, next);
            }
            if (mouseX > -9000) {
                const mCol = Math.floor(mouseX / CELL);
                const mRow = Math.floor(mouseY / CELL);
                if (mRow >= 0 && mRow < rows && mCol >= 0 && mCol < cols) {
                    active.set(mRow * cols + mCol, 1.0);
                }
            }
            ctx.setTransform(1, 0, 0, 1, 0, 0);
            ctx.drawImage(off, 0, 0);
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            for (const [idx, b] of active) {
                const r = Math.floor(idx / cols), c = idx % cols;
                ctx.fillStyle = `rgba(${HL_R},${HL_G},${HL_B},${(b * HL_PEAK_A).toFixed(3)})`;
                ctx.fillRect(c * CELL, r * CELL, CELL, CELL);
            }
            animId = requestAnimationFrame(loop);
        };

        animId = requestAnimationFrame(loop);
        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseleave', onMouseLeave);
        window.addEventListener('resize', resize);

        return () => {
            cancelAnimationFrame(animId);
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseleave', onMouseLeave);
            window.removeEventListener('resize', resize);
        };
    }, []);

    return (
        <canvas
            ref={canvasRef}
            style={{ position: 'fixed', top: 0, left: 0, width: '100%', height: '100%', zIndex: 0, pointerEvents: 'none' }}
        />
    );
}

/* ─────────────────────────────────────────────
   Cycling Prompt Suggestions
   ───────────────────────────────────────────── */
function CyclingPrompts() {
    const [index, setIndex] = useState(0);
    const [phase, setPhase] = useState<'in' | 'out'>('in');

    useEffect(() => {
        let innerTid: ReturnType<typeof setTimeout> | undefined;
        const timer = setInterval(() => {
            setPhase('out');
            innerTid = setTimeout(() => { setIndex((prev) => (prev + 1) % PROMPTS.length); setPhase('in'); }, 450);
        }, 3800);
        return () => { clearInterval(timer); if (innerTid !== undefined) clearTimeout(innerTid); };
    }, []);

    return (
        <span className={`cycling-prompt ${phase === 'in' ? 'prompt-slide-in' : 'prompt-slide-out'}`}>
            {PROMPTS[index]}
        </span>
    );
}

/* ─────────────────────────────────────────────
   Logo SVG (Stych bracket mark)
   ───────────────────────────────────────────── */
function BrandPlate() {
    return (
        <span className="lp-brand-plate">
            <svg viewBox="12 12 96 96" style={{ width: '72%', height: '72%', display: 'block' }} aria-hidden="true">
                <g fill="none" stroke="#f8f8f5" strokeWidth="2.4" strokeLinecap="butt" strokeLinejoin="miter" strokeMiterlimit="2">
                    <path d="M35 25 H23 V95 H35" />
                    <path d="M85 25 H97 V95 H85" />
                </g>
                <text x="60" y="57" textAnchor="middle" dominantBaseline="central"
                    fontFamily="'Space Grotesk', sans-serif" fontWeight="700" fontSize="73" fill="#f8f8f5">s</text>
            </svg>
        </span>
    );
}

/* ─────────────────────────────────────────────
   Landing Page
   ───────────────────────────────────────────── */
export default function Landing() {
    const navigate = useNavigate();
    const { isAuthenticated, isCheckingAuth, user } = useAuthStore();
    const isAuthReady = !isCheckingAuth;
    const canUseAuthenticatedActions = isAuthReady && isAuthenticated;

    const setPendingQuery = useStore((s) => s.setPendingQuery);
    const setUploadedFile = useStore((s) => s.setUploadedFile);
    const sessionId = useStore((s) => s.sessionId);
    const chatTheme = useStore((s) => s.chatTheme);
    const toggleChatTheme = useStore((s) => s.toggleChatTheme);
    const isDark = chatTheme === 'dark';

    const [query, setQuery] = useState('');
    const [selectedFile, setSelectedFile] = useState<File | null>(null);
    const [isUploading, setIsUploading] = useState(false);
    const [uploadError, setUploadError] = useState<string | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    const [showAuthModal, setShowAuthModal] = useState(false);
    const [authError, setAuthError] = useState<string | null>(null);
    const [isAuthLoading, setIsAuthLoading] = useState(false);
    const [pendingAction, setPendingAction] = useState<'query' | 'upload' | null>(null);

    /* ── File handling ── */
    const handleFileSelect = useCallback(
        (e: React.ChangeEvent<HTMLInputElement>) => {
            const file = e.target.files?.[0];
            if (!file) return;
            if (!canUseAuthenticatedActions) {
                setPendingAction('upload');
                setShowAuthModal(true);
                if (fileInputRef.current) fileInputRef.current.value = '';
                return;
            }
            setSelectedFile(file);
        },
        [canUseAuthenticatedActions]
    );

    const handleUploadAndGo = useCallback(async () => {
        if (!selectedFile) return;
        setIsUploading(true);
        setUploadError(null);
        try {
            const data = await uploadFile(selectedFile, sessionId || undefined);
            setUploadedFile({ file_id: data.file_id, filename: data.filename, rows: data.rows, columns: data.columns });
            if (query.trim()) setPendingQuery(query.trim());
            navigate('/chat');
        } catch (err: unknown) {
            const detail = (err as any)?.response?.data?.detail;
            setUploadError(detail || 'Upload failed. Please check the file and try again.');
        } finally {
            setIsUploading(false);
        }
    }, [selectedFile, query, sessionId, navigate, setUploadedFile, setPendingQuery]);

    const handleSendQuery = useCallback(() => {
        if (!query.trim() && !selectedFile) return;
        if (!canUseAuthenticatedActions) {
            setPendingAction('query');
            setShowAuthModal(true);
            return;
        }
        if (selectedFile) { handleUploadAndGo(); }
        else { setPendingQuery(query.trim()); navigate('/chat'); }
    }, [query, selectedFile, canUseAuthenticatedActions, navigate, setPendingQuery, handleUploadAndGo]);

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSendQuery(); }
    };

    const handleFileClick = () => {
        if (!canUseAuthenticatedActions) { setPendingAction('upload'); setShowAuthModal(true); }
        else { fileInputRef.current?.click(); }
    };

    const handleTryStych = () => {
        if (canUseAuthenticatedActions) navigate('/chat');
        else { setPendingAction('query'); setShowAuthModal(true); }
    };

    /* ── Google OAuth ── */
    const handleGoogleSignIn = async () => {
        setIsAuthLoading(true);
        setAuthError(null);
        try {
            await googleSignIn();
            if (pendingAction === 'query' && query.trim()) { setPendingQuery(query.trim()); navigate('/chat'); }
            else if (pendingAction === 'upload') { fileInputRef.current?.click(); }
            else { navigate('/chat'); }
        } catch (err: unknown) {
            const message = err instanceof Error && 'response' in err
                ? (err as Record<string, Record<string, Record<string, string>>>).response?.data?.detail
                : undefined;
            setAuthError(message || 'Authentication failed. Please try again.');
        } finally {
            setIsAuthLoading(false);
            setPendingAction(null);
        }
    };

    /* ── Render ── */
    return (
        <div style={{ background: 'var(--c-paper)', color: 'var(--c-ink)', position: 'relative' }}>
            {isDark && <GridBackground />}

            {/* ═══ NAV ═══ */}
            <nav className="lp-nav">
                <div className="lp-nav-inner">
                    <button className="lp-brand" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}>
                        <BrandPlate />
                        <span className="lp-brand-word">Stych</span>
                    </button>
                    <div className="lp-nav-right">
                        <button className="lp-toggle" onClick={toggleChatTheme} aria-label="Toggle theme">
                            {isDark
                                ? <Sun style={{ width: 15, height: 15 }} />
                                : <Moon style={{ width: 15, height: 15 }} />}
                        </button>
                        {canUseAuthenticatedActions ? (
                            <button className="lp-btn" onClick={() => navigate('/chat')} style={{ gap: 8 }}>
                                {user?.profile_picture && (
                                    <img src={user.profile_picture} alt="" style={{ width: 18, height: 18, borderRadius: '50%' }} referrerPolicy="no-referrer" />
                                )}
                                Go to Chat
                            </button>
                        ) : (
                            <button className="lp-btn" onClick={() => navigate('/login')}>
                                Try Stych
                            </button>
                        )}
                    </div>
                </div>
            </nav>

            {/* ═══ HERO ═══ */}
            <header className="lp-hero" style={{ position: 'relative', zIndex: 10 }}>
                <div className="lp-wrap">
                    <div className="lp-hero-grid">
                        {/* Left — copy + search bar */}
                        <div>
                            <span className="lp-status">
                                <i className="lp-dot" />
                                Live · Connect any CSV or warehouse
                            </span>
                            <h1 className="lp-h1">
                                Talk to your data.<br /><em>It cleans itself.</em>
                            </h1>
                            <p className="lp-lede">
                                Stych is an agentic interface for working with data. Point it at a dataset and ask — fill missing values, drop duplicates, flag outliers, summarize trends. You stay in the conversation; the agent does the tedious work.
                            </p>

                            {/* ── Functional search bar ── */}
                            <div style={{ marginTop: 28 }}>
                                {/* Selected-file chip */}
                                {selectedFile && (
                                    <div style={{ marginBottom: 10 }}>
                                        <div style={{
                                            display: 'inline-flex', alignItems: 'center', gap: 8,
                                            borderRadius: 999, padding: '6px 14px', fontSize: 13,
                                            background: 'var(--c-signal-dim)', border: '1px solid var(--c-glass-border)',
                                            color: 'var(--c-ink)',
                                        }}>
                                            <FileText style={{ width: 14, height: 14, color: 'var(--c-signal)' }} />
                                            <span style={{ maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{selectedFile.name}</span>
                                            <button onClick={() => setSelectedFile(null)} style={{ opacity: 0.5, background: 'none', border: 'none', cursor: 'pointer', color: 'var(--c-ink)', display: 'flex', alignItems: 'center' }}>
                                                <X style={{ width: 13, height: 13 }} />
                                            </button>
                                        </div>
                                    </div>
                                )}
                                {/* Upload error */}
                                {uploadError && (
                                    <div style={{ marginBottom: 10 }}>
                                        <div style={{
                                            display: 'inline-flex', alignItems: 'center', gap: 8,
                                            borderRadius: 999, padding: '6px 14px', fontSize: 13,
                                            background: 'rgba(220,38,38,0.1)', border: '1px solid rgba(220,38,38,0.25)',
                                            color: 'var(--c-danger)',
                                        }}>
                                            <span>{uploadError}</span>
                                            <button onClick={() => setUploadError(null)} style={{ opacity: 0.6, background: 'none', border: 'none', cursor: 'pointer', color: 'inherit', display: 'flex', alignItems: 'center' }}>
                                                <X style={{ width: 13, height: 13 }} />
                                            </button>
                                        </div>
                                    </div>
                                )}
                                {/* Gradient-border input card */}
                                <div className="lp-search-border">
                                    <div className="lp-search-inner">
                                        {/* Text input row */}
                                        <div style={{ position: 'relative', display: 'flex', alignItems: 'center', padding: '16px 16px 10px' }}>
                                            {!query && (
                                                <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', padding: '0 16px', pointerEvents: 'none', overflow: 'hidden' }}>
                                                    <CyclingPrompts />
                                                </div>
                                            )}
                                            <input
                                                type="text"
                                                value={query}
                                                onChange={(e) => setQuery(e.target.value)}
                                                onKeyDown={handleKeyDown}
                                                style={{
                                                    width: '100%', background: 'transparent', border: 'none', outline: 'none',
                                                    fontSize: 16, color: 'var(--c-ink)', position: 'relative', zIndex: 1,
                                                    fontFamily: 'inherit',
                                                }}
                                            />
                                        </div>
                                        {/* Action row */}
                                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 10px 10px' }}>
                                            <div style={{ position: 'relative' }} className="group">
                                                <button
                                                    onClick={handleFileClick}
                                                    style={{
                                                        width: 36, height: 36, borderRadius: '50%', border: 'none', background: 'none',
                                                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                                                        cursor: 'pointer', color: 'var(--c-ink-2)', transition: 'color 0.2s',
                                                    }}
                                                    onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--c-signal)')}
                                                    onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--c-ink-2)')}
                                                >
                                                    <Paperclip style={{ width: 18, height: 18 }} />
                                                </button>
                                                <input ref={fileInputRef} type="file" className="hidden" accept=".csv,.xlsx,.json" onChange={handleFileSelect} />
                                                <div className="file-tooltip">Upload your data file (CSV, Excel, or JSON) to have it analyzed by Stych</div>
                                            </div>
                                            <button
                                                onClick={handleSendQuery}
                                                disabled={(!query.trim() && !selectedFile) || isUploading}
                                                style={{
                                                    width: 36, height: 36, borderRadius: '50%', border: 'none',
                                                    background: 'var(--c-signal)', color: '#fff',
                                                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                                                    cursor: 'pointer', boxShadow: '0 4px 16px var(--c-signal-glow)',
                                                    opacity: (!query.trim() && !selectedFile) || isUploading ? 0.3 : 1,
                                                    transition: 'opacity 0.2s',
                                                }}
                                            >
                                                {isUploading
                                                    ? <div className="animate-spin" style={{ width: 14, height: 14, borderRadius: '50%', border: '2px solid #fff', borderTopColor: 'transparent' }} />
                                                    : <ArrowUp style={{ width: 16, height: 16 }} />}
                                            </button>
                                        </div>
                                    </div>
                                </div>
                            </div>

                            <p className="lp-hero-note">CSV · Postgres · Snowflake · BigQuery · Sheets</p>
                        </div>

                        {/* Right — app workbench mock */}
                        <div className="lp-app" role="img" aria-label="Stych data workbench preview">
                            <div className="lp-app-bar">
                                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                                    <span className="lp-app-file"><span className="faint">~/data/</span>sales_q3.csv</span>
                                </div>
                                <span className="lp-app-status"><i className="lp-dot" style={{ width: 6, height: 6 }} /> 8,412 rows · cleaning</span>
                            </div>
                            <div className="lp-app-body">
                                {/* Chat column */}
                                <div className="lp-chat">
                                    <div className="lp-msg lp-msg-me">
                                        <div className="lp-bub lp-bub-me">
                                            Clean <strong>sales_q3.csv</strong> — fill missing values, drop duplicates, and flag any outliers.
                                        </div>
                                    </div>
                                    <div className="lp-msg">
                                        <span className="lp-av"><span className="lp-av-s">s</span></span>
                                        <div className="lp-bub lp-bub-ai">
                                            Profiled <strong>8,412 rows</strong> across 5 columns. Here's what I found:
                                        </div>
                                    </div>
                                    <div className="lp-msg" style={{ paddingLeft: 36 }}>
                                        <div className="lp-result" style={{ width: '100%' }}>
                                            <div className="lp-result-row"><span className="lp-result-k">Missing values</span><span className="lp-result-v">214 → filled</span></div>
                                            <div className="lp-result-row"><span className="lp-result-k">Duplicate rows</span><span className="lp-result-v">38 → removed</span></div>
                                            <div className="lp-result-row"><span className="lp-result-k">Outliers</span><span className="lp-result-v lp-result-amber">6 → flagged</span></div>
                                        </div>
                                    </div>
                                    <div className="lp-msg">
                                        <span className="lp-av"><span className="lp-av-s">s</span></span>
                                        <div className="lp-bub lp-bub-ai">
                                            Filled missing revenue with the <strong>regional median</strong>. 6 outliers look like data-entry errors — want me to correct or keep them?
                                        </div>
                                    </div>
                                    <div className="lp-chat-ph" style={{ marginTop: 'auto' }}>
                                        <span className="lp-chat-ph-text">Ask Stych to do something…</span>
                                        <span className="lp-chat-send">
                                            <svg viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2" style={{ width: 13, height: 13 }}>
                                                <path d="M5 12h13M13 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
                                            </svg>
                                        </span>
                                    </div>
                                </div>

                                {/* Table column */}
                                <div className="lp-grid-side">
                                    <div className="lp-grid-head">
                                        <span className="lp-grid-title">Preview · live edits</span>
                                        <span className="lp-legend">
                                            <span><i className="lp-legend-i" style={{ background: 'var(--c-signal)' }} />fixed</span>
                                            <span><i className="lp-legend-i" style={{ background: '#e8a13a' }} />flagged</span>
                                        </span>
                                    </div>
                                    <table className="lp-dtable">
                                        <thead>
                                            <tr><th>Date</th><th>Region</th><th>Revenue</th><th>Units</th><th>Status</th></tr>
                                        </thead>
                                        <tbody>
                                            <tr><td>07-02</td><td>West</td><td>$48,210</td><td>312</td><td>ok</td></tr>
                                            <tr><td>07-02</td><td>East</td><td className="lp-cell-fix">$51,470<span className="lp-tag">↳ filled</span></td><td>340</td><td>ok</td></tr>
                                            <tr><td>07-03</td><td>South</td><td>$39,905</td><td>266</td><td>ok</td></tr>
                                            <tr className="lp-row-dupe"><td>07-03</td><td>South</td><td>$39,905</td><td>266</td><td className="keep">dup</td></tr>
                                            <tr><td>07-04</td><td>West</td><td className="lp-cell-flag">$612,000<span className="lp-tag">⚠ outlier</span></td><td>318</td><td>review</td></tr>
                                            <tr><td>07-05</td><td>North</td><td>$44,180</td><td className="lp-cell-fix">291<span className="lp-tag">↳ filled</span></td><td>ok</td></tr>
                                            <tr><td>07-06</td><td>East</td><td>$52,940</td><td>355</td><td>ok</td></tr>
                                        </tbody>
                                    </table>
                                    <div className="lp-audit">
                                        <span className="lp-audit-l"><b>258 changes</b> applied · all reversible</span>
                                        <span className="lp-audit-cta">View audit trail →</span>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </header>

            {/* ═══ STATS STRIP ═══ */}
            <div className="lp-strip" style={{ position: 'relative', zIndex: 10 }}>
                <div className="lp-wrap">
                    <div className="lp-strip-inner">
                        <span className="lp-strip-lead">From raw dump to analysis-ready — in one conversation.</span>
                        <div className="lp-strip-stats">
                            <div><div className="lp-stat-n">8,412</div><div className="lp-stat-l">Rows profiled / sec</div></div>
                            <div><div className="lp-stat-n">258</div><div className="lp-stat-l">Fixes, fully reversible</div></div>
                            <div><div className="lp-stat-n">5+</div><div className="lp-stat-l">Sources connected</div></div>
                            <div><div className="lp-stat-n">0</div><div className="lp-stat-l">Scripts to write</div></div>
                        </div>
                    </div>
                </div>
            </div>

            {/* ═══ FEATURES ═══ */}
            <section className="lp-block" style={{ position: 'relative', zIndex: 10 }}>
                <div className="lp-wrap">
                    <div className="lp-block-head">
                        <span className="lp-kicker">What Stych does</span>
                        <h2 className="lp-block-h2">The tedious 80% of data work, handled.</h2>
                        <p className="lp-block-p">Stych profiles messy datasets, proposes fixes, and reconciles conflicts across sources — turning raw, inconsistent records into clean, analysis-ready data while you stay in the loop.</p>
                    </div>
                    <div className="lp-feat">
                        <div className="lp-fcell">
                            <span className="lp-fic">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" style={{ width: 22, height: 22 }}>
                                    <circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" strokeLinecap="round" />
                                </svg>
                            </span>
                            <h3 className="lp-fcell-h3">Profiles instantly</h3>
                            <p className="lp-fcell-p">Point Stych at any dataset and it maps types, ranges and distributions — surfacing missing values, duplicates and anomalies in seconds.</p>
                            <span className="lp-fcell-meta">Automatic anomaly detection</span>
                        </div>
                        <div className="lp-fcell">
                            <span className="lp-fic">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" style={{ width: 22, height: 22 }}>
                                    <path d="M3 7l4-4 4 4M7 3v13a2 2 0 0 0 2 2h12" strokeLinecap="round" strokeLinejoin="round" />
                                    <path d="M21 17l-4 4-4-4" strokeLinecap="round" strokeLinejoin="round" />
                                </svg>
                            </span>
                            <h3 className="lp-fcell-h3">Fixes on its own</h3>
                            <p className="lp-fcell-p">Fill, dedupe, normalize and reconcile across sources — self-directed, but every action is proposed in plain language for you to approve.</p>
                            <span className="lp-fcell-meta">Self-directed cleaning</span>
                        </div>
                        <div className="lp-fcell">
                            <span className="lp-fic">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" style={{ width: 22, height: 22 }}>
                                    <path d="M5 4h11l3 3v13H5z" strokeLinejoin="round" />
                                    <path d="M8 11h8M8 15h6" strokeLinecap="round" />
                                </svg>
                            </span>
                            <h3 className="lp-fcell-h3">Full audit trail</h3>
                            <p className="lp-fcell-p">Every change is logged, explained and reversible. Hand a teammate a clean dataset and the exact record of how it got there.</p>
                            <span className="lp-fcell-meta">Reversible · reportable</span>
                        </div>
                    </div>
                </div>
            </section>

            {/* ═══ HOW IT WORKS ═══ */}
            <section id="how" className="lp-block" style={{ paddingTop: 0, position: 'relative', zIndex: 10 }}>
                <div className="lp-wrap">
                    <div className="lp-mandate">
                        <div>
                            <span className="lp-kicker" style={{ marginBottom: 16 }}>The loop</span>
                            <h2 className="lp-mandate-h2">You stay in the conversation. The agent does the work.</h2>
                            <p className="lp-mandate-p">No scripts, no notebooks, no brittle pipelines. Ask in plain language, watch the edits land live, and approve what ships. Stych keeps you in control while removing the grind.</p>
                        </div>
                        <div className="lp-steps">
                            <div className="lp-step">
                                <span className="lp-step-n">01</span>
                                <div>
                                    <h4 className="lp-step-h4">Connect a source</h4>
                                    <p className="lp-step-p">Drop a CSV or connect Postgres, Snowflake, BigQuery or Sheets — read-only by default.</p>
                                </div>
                            </div>
                            <div className="lp-step">
                                <span className="lp-step-n">02</span>
                                <div>
                                    <h4 className="lp-step-h4">Ask in plain language</h4>
                                    <p className="lp-step-p">"Fill missing values, flag outliers, summarize Q3 by region." Stych profiles and proposes.</p>
                                </div>
                            </div>
                            <div className="lp-step">
                                <span className="lp-step-n">03</span>
                                <div>
                                    <h4 className="lp-step-h4">Watch edits land live</h4>
                                    <p className="lp-step-p">Fixes highlight in the preview as they apply — you see exactly what changed and why.</p>
                                </div>
                            </div>
                            <div className="lp-step">
                                <span className="lp-step-n">04</span>
                                <div>
                                    <h4 className="lp-step-h4">Approve &amp; export</h4>
                                    <p className="lp-step-p">Ship clean, analysis-ready data with a full, reversible audit trail attached.</p>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </section>

            {/* ═══ CTA ═══ */}
            <section id="start" className="lp-block" style={{ paddingTop: 0, position: 'relative', zIndex: 10 }}>
                <div className="lp-wrap">
                    <div className="lp-cta">
                        <span className="lp-kicker lp-cta-kicker">Free to start</span>
                        <h2 className="lp-cta-h2">Point Stych at your messiest dataset.</h2>
                        <p className="lp-cta-p">Connect a source, ask in plain language, and watch raw records turn into clean, analysis-ready data — without writing a line of code.</p>
                        <div className="lp-cta-row">
                            <button className="lp-cta-btn" onClick={handleTryStych}>
                                Try Stych →
                            </button>
                            <button className="lp-cta-btn lp-cta-btn-ghost" onClick={() => navigate('/login')}>
                                Sign in
                            </button>
                        </div>
                    </div>
                </div>
            </section>

            {/* ═══ FOOTER ═══ */}
            <footer className="lp-foot" style={{ position: 'relative', zIndex: 10 }}>
                <div className="lp-wrap">
                    <div className="lp-foot-inner">
                        <div>
                            <button className="lp-brand" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}>
                                <BrandPlate />
                                <span className="lp-brand-word">Stych</span>
                            </button>
                            <p className="lp-foot-meta">Stych is an agentic data workbench operated by Stych. Connect read-only and keep a full audit trail of every change.</p>
                        </div>
                        <span className="lp-foot-fam">© {new Date().getFullYear()} Stych</span>
                    </div>
                </div>
            </footer>

            {/* ═══ AUTH MODAL ═══ */}
            {showAuthModal && (
                <div className="fixed inset-0 z-[100] flex items-center justify-center">
                    <div
                        className="absolute inset-0 bg-black/50 backdrop-blur-sm"
                        onClick={() => { setShowAuthModal(false); setPendingAction(null); }}
                    />
                    <div
                        className="relative rounded-2xl shadow-2xl p-8 max-w-sm w-full mx-4"
                        style={{ background: 'var(--c-slate)', border: '1px solid var(--c-glass-border)' }}
                    >
                        <button
                            onClick={() => { setShowAuthModal(false); setPendingAction(null); }}
                            className="absolute top-4 right-4 transition-opacity opacity-40 hover:opacity-80"
                            style={{ color: 'var(--c-ink)', background: 'none', border: 'none', cursor: 'pointer' }}
                        >
                            <X className="w-5 h-5" />
                        </button>
                        <div className="text-center mb-6">
                            <div className="w-14 h-14 rounded-full flex items-center justify-center mx-auto mb-4"
                                style={{ background: 'var(--c-signal-dim)', border: '1px solid var(--c-glass-border)' }}>
                                <Bot className="w-8 h-8" style={{ color: 'var(--c-signal)' }} />
                            </div>
                            <h3 className="text-xl font-bold mb-1" style={{ color: 'var(--c-ink)', fontFamily: 'Space Grotesk, sans-serif' }}>
                                Sign in to continue
                            </h3>
                            <p className="text-sm" style={{ color: 'var(--c-ink-2)' }}>
                                {pendingAction === 'upload'
                                    ? 'Sign in with Google to upload files and start analyzing'
                                    : 'Sign in with Google to chat with Stych'}
                            </p>
                        </div>
                        {authError && (
                            <div className="mb-4 p-3 rounded-lg text-sm text-center"
                                style={{ background: 'rgba(220,38,38,0.1)', border: '1px solid rgba(220,38,38,0.2)', color: 'var(--c-danger)' }}>
                                {authError}
                            </div>
                        )}
                        <div className="flex justify-center">
                            {isAuthLoading ? (
                                <div className="flex items-center space-x-3 py-3">
                                    <div className="w-5 h-5 border-2 border-t-transparent rounded-full animate-spin"
                                        style={{ borderColor: 'var(--c-signal)', borderTopColor: 'transparent' }} />
                                    <span className="text-sm" style={{ color: 'var(--c-ink-2)' }}>Signing in…</span>
                                </div>
                            ) : (
                                <button
                                    onClick={handleGoogleSignIn}
                                    className="flex items-center justify-center gap-3 px-6 py-3 rounded-full text-sm font-medium transition-all duration-200 w-72"
                                    style={{ background: 'var(--c-glass)', border: '1px solid var(--c-glass-border)', color: 'var(--c-ink)', cursor: 'pointer' }}
                                >
                                    <svg width="18" height="18" viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg">
                                        <path d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z" fill="#4285F4" />
                                        <path d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z" fill="#34A853" />
                                        <path d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z" fill="#FBBC05" />
                                        <path d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z" fill="#EA4335" />
                                    </svg>
                                    Sign in with Google
                                </button>
                            )}
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
