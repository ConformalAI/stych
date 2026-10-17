import type { ChartTheme } from './compileShared';
import type { TexturePattern, VizSpec } from './types';

export interface DecalOption {
    symbol?: string;
    symbolSize?: number;
    color: string;
    dashArrayX: number | Array<number | number[]>;
    dashArrayY: number | number[];
    rotation?: number;
}

export function textureDecalOf(spec: VizSpec, theme: ChartTheme): DecalOption | undefined {
    const texture = spec.config?.texture;
    if (!texture?.enabled) return undefined;
    const color = theme.isDark ? 'rgba(255,255,255,0.38)' : 'rgba(15,23,42,0.34)';
    const shared = { color, symbolSize: 1 };
    switch (texture.pattern) {
        case 'horizontal':
            return { ...shared, dashArrayX: [1, 0], dashArrayY: [2, 5], rotation: 0 };
        case 'vertical':
            return {
                ...shared,
                dashArrayX: [1, 0],
                dashArrayY: [2, 5],
                rotation: Math.PI / 2,
            };
        case 'dots':
            return {
                ...shared,
                symbol: 'circle',
                symbolSize: 0.8,
                dashArrayX: [
                    [8, 8],
                    [0, 8, 8, 0],
                ],
                dashArrayY: [6, 0],
            };
        case 'grid':
            return {
                ...shared,
                symbol: 'rect',
                symbolSize: 0.8,
                dashArrayX: [
                    [6, 6],
                    [0, 6, 6, 0],
                ],
                dashArrayY: [6, 0],
            };
        case 'crosshatch':
            return {
                ...shared,
                dashArrayX: [
                    [1, 0],
                    [1, 6],
                ],
                dashArrayY: [1, 0, 6, 0],
                rotation: Math.PI / 4,
            };
        case 'diagonal':
            return {
                ...shared,
                dashArrayX: [1, 0],
                dashArrayY: [4, 3],
                rotation: -Math.PI / 4,
            };
    }
}

export function textureLineTypeOf(spec: VizSpec): 'solid' | 'dashed' | 'dotted' {
    const texture = spec.config?.texture;
    if (!texture?.enabled) return 'solid';
    return texture.pattern === 'dots' ? 'dotted' : 'dashed';
}

export function textureSymbolOf(spec: VizSpec): string | undefined {
    const texture = spec.config?.texture;
    if (!texture?.enabled) return undefined;
    const symbols: Record<TexturePattern, string> = {
        diagonal: 'triangle',
        horizontal: 'roundRect',
        vertical: 'rect',
        dots: 'circle',
        grid: 'diamond',
        crosshatch: 'pin',
    };
    return symbols[texture.pattern];
}
