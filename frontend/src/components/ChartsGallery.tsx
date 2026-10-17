import ChartRenderer, { ChartSpec } from './ChartRenderer';
import { useStore } from '../store/useStore';

interface ChartsGalleryProps {
    charts: ChartSpec[];
}

export default function ChartsGallery({ charts }: ChartsGalleryProps) {
    const chatTheme = useStore((s) => s.chatTheme);
    const isDark = chatTheme === 'dark';

    if (!charts || charts.length === 0) {
        return null;
    }

    return (
        <div
            className={`my-3 w-[640px] max-w-full rounded-2xl border p-3 ${
                isDark ? 'bg-white/[0.03] border-white/[0.08]' : 'bg-slate-50 border-slate-200'
            }`}
        >
            <p
                className={`text-xs font-semibold mb-2 ${isDark ? 'text-white/60' : 'text-slate-600'}`}
            >
                Initial EDA Visualizations ({charts.length})
            </p>
            <div className="max-h-[70vh] overflow-auto pr-1">
                {charts.map((chart, idx) => {
                    const isWide = (chart.data?.length ?? 0) > 12;
                    return (
                        <div
                            key={`${chart.title ?? 'chart'}-${idx}`}
                            style={{ width: isWide ? 940 : 640 }}
                        >
                            <ChartRenderer spec={chart} />
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
