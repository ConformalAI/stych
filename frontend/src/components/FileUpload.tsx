import { useCallback, useEffect, useRef, useState } from 'react';
import { useDropzone } from 'react-dropzone';
import {
    UploadCloud,
    FileText,
    Loader2,
    CheckCircle2,
    AlertCircle,
    FileSpreadsheet,
    Braces,
    X,
} from 'lucide-react';
import { useMutation } from '@tanstack/react-query';
import { deleteSource, uploadFile } from '../services/api';
import { forgetRetiredSource } from '../sourceRemoved';
import { useStore } from '../store/useStore';
import { setSessionId } from '../services/storage';

interface FileUploadProps {
    onComplete?: () => void;
}

const FORMAT_INFO = [
    { icon: FileText, ext: 'CSV', desc: 'Comma-separated values' },
    { icon: FileSpreadsheet, ext: 'XLSX', desc: 'Excel spreadsheet' },
    { icon: Braces, ext: 'JSON', desc: 'JSON array or object' },
];

const STEPS = [
    'Files uploaded securely',
    'AI scans structure & types',
    'Chat with your data instantly',
];

export default function FileUpload({ onComplete }: FileUploadProps = {}) {
    const sessionId = useStore((s) => s.sessionId);
    const uploadedFiles = useStore((s) => s.uploadedFiles);
    const addUploadedFile = useStore((s) => s.addUploadedFile);
    const setError = useStore((s) => s.setError);
    const chatTheme = useStore((s) => s.chatTheme);
    const notifySessionActivity = useStore((s) => s.notifySessionActivity);
    const isDark = chatTheme === 'dark';
    const [uploadStatus, setUploadStatus] = useState<'idle' | 'uploading' | 'success' | 'error'>(
        'idle'
    );
    const [uploadingName, setUploadingName] = useState('');
    const [confirmingId, setConfirmingId] = useState<string | null>(null);
    const [deletingId, setDeletingId] = useState<string | null>(null);
    const completionTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const confirmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    useEffect(
        () => () => {
            if (completionTimerRef.current) clearTimeout(completionTimerRef.current);
            if (confirmTimerRef.current) clearTimeout(confirmTimerRef.current);
        },
        []
    );

    const armDelete = (fileId: string) => {
        setConfirmingId(fileId);
        if (confirmTimerRef.current) clearTimeout(confirmTimerRef.current);
        confirmTimerRef.current = setTimeout(() => setConfirmingId(null), 3500);
    };

    const handleDelete = async (fileId: string) => {
        if (confirmTimerRef.current) clearTimeout(confirmTimerRef.current);
        setConfirmingId(null);
        if (!sessionId || deletingId) return;
        setDeletingId(fileId);
        try {
            await deleteSource(sessionId, { kind: 'dataset', id: fileId, file_id: fileId });
            forgetRetiredSource(fileId);
        } catch (error: any) {
            setError(error.response?.data?.detail || 'Failed to delete dataset.');
        } finally {
            setDeletingId(null);
        }
    };

    const uploadMutation = useMutation({
        mutationFn: (file: File) => {
            if (!sessionId) throw new Error('No active session. Please refresh the page.');
            return uploadFile(file, sessionId);
        },
        onSuccess: (data) => {
            // The upload endpoint creates the durable session before returning.
            // Keep the client aligned if it had to mint a session ID server-side.
            if (data.session_id && data.session_id !== sessionId) {
                useStore.getState().setSessionId(data.session_id);
                setSessionId(data.session_id);
            }
            addUploadedFile({
                file_id: data.file_id,
                filename: data.filename,
                rows: data.rows,
                columns: data.columns,
            });
            notifySessionActivity(data.session_id);
        },
        onError: (error: any, file: File) => {
            setError(error.response?.data?.detail || `Upload failed for ${file.name}`);
        },
    });

    const processQueue = useCallback(
        async (files: File[]) => {
            // Clear any pending completion timer before starting a new queue.
            if (completionTimerRef.current) {
                clearTimeout(completionTimerRef.current);
                completionTimerRef.current = null;
            }
            setUploadStatus('uploading');
            let allOk = true;

            for (const file of files) {
                try {
                    setUploadingName(file.name);
                    await uploadMutation.mutateAsync(file);
                } catch {
                    allOk = false;
                }
            }

            if (allOk) {
                setUploadStatus('success');
                completionTimerRef.current = setTimeout(() => {
                    setUploadStatus('idle');
                    onComplete?.();
                }, 1500);
            } else {
                setUploadStatus('error');
                completionTimerRef.current = setTimeout(() => setUploadStatus('idle'), 3000);
            }
        },
        [uploadMutation, onComplete]
    );

    const onDrop = useCallback(
        (acceptedFiles: File[]) => {
            if (acceptedFiles.length > 0) {
                processQueue(acceptedFiles);
            }
        },
        [processQueue]
    );

    const { getRootProps, getInputProps, isDragActive } = useDropzone({
        onDrop,
        accept: {
            'text/csv': ['.csv'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['.xlsx'],
            'application/vnd.ms-excel': ['.xls'],
            'application/json': ['.json'],
        },
        multiple: true,
        maxSize: 50 * 1024 * 1024,
        disabled: uploadStatus === 'uploading' || uploadStatus === 'success',
    });

    const isIdle = uploadStatus === 'idle';

    return (
        <div className="space-y-5">
            {/* Header */}
            <div>
                <h2 className={`text-xl font-bold ${isDark ? 'text-white' : 'text-slate-800'}`}>
                    Upload your datasets
                </h2>
                <p className={`text-sm mt-0.5 ${isDark ? 'text-white/40' : 'text-slate-400'}`}>
                    CSV, Excel, or JSON · up to 50 MB each · multiple files supported
                </p>
            </div>

            {/* Already uploaded files */}
            {uploadedFiles.length > 0 && (
                <div className="space-y-1.5">
                    <p
                        className={`text-xs font-semibold uppercase tracking-widest ${isDark ? 'text-white/30' : 'text-slate-400'}`}
                    >
                        Uploaded ({uploadedFiles.length})
                    </p>
                    {uploadedFiles.map((f) => (
                        <div
                            key={f.file_id}
                            className={`flex items-center justify-between rounded-xl px-3.5 py-2 border ${
                                isDark
                                    ? 'bg-white/[0.04] border-white/[0.08]'
                                    : 'bg-gray-50 border-gray-200'
                            }`}
                        >
                            <div className="flex items-center gap-2.5 min-w-0">
                                <div
                                    className={`w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0 ${isDark ? 'bg-emerald-500/15' : 'bg-emerald-100'}`}
                                >
                                    <CheckCircle2
                                        className={`w-3.5 h-3.5 ${isDark ? 'text-emerald-400' : 'text-emerald-600'}`}
                                    />
                                </div>
                                <div className="min-w-0">
                                    <p
                                        className={`text-sm font-medium truncate ${isDark ? 'text-white/80' : 'text-slate-700'}`}
                                    >
                                        {f.filename}
                                    </p>
                                    <p
                                        className={`text-xs ${isDark ? 'text-white/30' : 'text-slate-400'}`}
                                    >
                                        {f.rows.toLocaleString()} rows · {f.columns} cols
                                    </p>
                                </div>
                            </div>
                            {deletingId === f.file_id ? (
                                <span className="w-6 h-6 flex items-center justify-center flex-shrink-0">
                                    <Loader2 className="w-3.5 h-3.5 animate-spin text-red-400" />
                                </span>
                            ) : confirmingId === f.file_id ? (
                                <div className="flex items-center gap-1 flex-shrink-0">
                                    <button
                                        type="button"
                                        onClick={() => handleDelete(f.file_id)}
                                        className="px-2 py-0.5 rounded-md text-[11px] font-semibold text-white bg-red-500 hover:bg-red-600 transition-colors"
                                    >
                                        Delete
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            if (confirmTimerRef.current)
                                                clearTimeout(confirmTimerRef.current);
                                            setConfirmingId(null);
                                        }}
                                        className={`w-6 h-6 rounded-md flex items-center justify-center transition-colors ${
                                            isDark
                                                ? 'text-white/40 hover:text-white/70 hover:bg-white/10'
                                                : 'text-slate-400 hover:text-slate-600 hover:bg-gray-100'
                                        }`}
                                    >
                                        <X className="w-3.5 h-3.5" />
                                    </button>
                                </div>
                            ) : (
                                <button
                                    type="button"
                                    onClick={() => armDelete(f.file_id)}
                                    title="Delete dataset"
                                    className={`w-6 h-6 rounded-md flex items-center justify-center flex-shrink-0 transition-colors ${
                                        isDark
                                            ? 'text-white/20 hover:text-red-400 hover:bg-red-500/10'
                                            : 'text-slate-300 hover:text-red-500 hover:bg-red-50'
                                    }`}
                                >
                                    <X className="w-3.5 h-3.5" />
                                </button>
                            )}
                        </div>
                    ))}
                </div>
            )}

            {/* Drop zone */}
            <div
                {...getRootProps()}
                className={`relative overflow-hidden rounded-2xl border-2 border-dashed cursor-pointer transition-all duration-200 ${
                    uploadStatus === 'uploading'
                        ? isDark
                            ? 'border-blue-500/40 bg-blue-500/5 cursor-wait'
                            : 'border-blue-300 bg-blue-50 cursor-wait'
                        : uploadStatus === 'success'
                          ? isDark
                              ? 'border-emerald-500/40 bg-emerald-500/5 cursor-default'
                              : 'border-emerald-400 bg-emerald-50 cursor-default'
                          : uploadStatus === 'error'
                            ? isDark
                                ? 'border-red-500/40 bg-red-500/5 cursor-default'
                                : 'border-red-400 bg-red-50 cursor-default'
                            : isDragActive
                              ? isDark
                                  ? 'border-blue-400/60 bg-blue-500/10 scale-[1.01]'
                                  : 'border-blue-400 bg-blue-50 scale-[1.01]'
                              : isDark
                                ? 'border-white/10 bg-white/[0.03] hover:border-blue-500/30 hover:bg-blue-500/5'
                                : 'border-gray-200 bg-gray-50 hover:border-blue-300 hover:bg-blue-50/40'
                }`}
            >
                <input {...getInputProps()} />

                <div className="flex flex-col items-center justify-center py-10 px-6 text-center">
                    {uploadStatus === 'uploading' ? (
                        <>
                            <div className="w-14 h-14 rounded-2xl bg-blue-500/15 flex items-center justify-center mb-4">
                                <Loader2 className="w-7 h-7 text-blue-400 animate-spin" />
                            </div>
                            <p
                                className={`text-base font-semibold ${isDark ? 'text-white' : 'text-slate-700'}`}
                            >
                                Uploading…
                            </p>
                            <p
                                className={`text-sm mt-1 truncate max-w-xs ${isDark ? 'text-white/40' : 'text-slate-400'}`}
                            >
                                {uploadingName}
                            </p>
                        </>
                    ) : uploadStatus === 'success' ? (
                        <>
                            <div className="w-14 h-14 rounded-2xl bg-emerald-500/15 flex items-center justify-center mb-4">
                                <CheckCircle2 className="w-7 h-7 text-emerald-400" />
                            </div>
                            <p
                                className={`text-base font-semibold ${isDark ? 'text-emerald-300' : 'text-emerald-600'}`}
                            >
                                All done!
                            </p>
                            <p
                                className={`text-sm mt-1 ${isDark ? 'text-emerald-400/70' : 'text-emerald-500'}`}
                            >
                                Redirecting you back…
                            </p>
                        </>
                    ) : uploadStatus === 'error' ? (
                        <>
                            <div className="w-14 h-14 rounded-2xl bg-red-500/15 flex items-center justify-center mb-4">
                                <AlertCircle className="w-7 h-7 text-red-400" />
                            </div>
                            <p
                                className={`text-base font-semibold ${isDark ? 'text-red-300' : 'text-red-600'}`}
                            >
                                Upload failed
                            </p>
                            <p
                                className={`text-sm mt-1 ${isDark ? 'text-red-400/70' : 'text-red-500'}`}
                            >
                                Click to try again
                            </p>
                        </>
                    ) : isDragActive ? (
                        <>
                            <div className="w-14 h-14 rounded-2xl bg-blue-500/20 flex items-center justify-center mb-4 animate-bounce">
                                <FileText className="w-7 h-7 text-blue-400" />
                            </div>
                            <p
                                className={`text-base font-semibold ${isDark ? 'text-blue-300' : 'text-blue-600'}`}
                            >
                                Drop your files!
                            </p>
                        </>
                    ) : (
                        <>
                            <div
                                className={`w-14 h-14 rounded-2xl flex items-center justify-center mb-4 border ${isDark ? 'bg-white/5 border-white/10' : 'bg-white border-gray-200 shadow-sm'}`}
                            >
                                <UploadCloud className="w-7 h-7 text-blue-400" />
                            </div>
                            <p
                                className={`text-base font-semibold ${isDark ? 'text-white' : 'text-slate-700'}`}
                            >
                                {uploadedFiles.length > 0
                                    ? 'Add more files'
                                    : 'Drag & drop your files'}
                            </p>
                            <p
                                className={`text-sm mt-1 mb-4 ${isDark ? 'text-white/40' : 'text-slate-400'}`}
                            >
                                {uploadedFiles.length > 0
                                    ? 'Drop additional datasets for linking'
                                    : 'or click to browse · select multiple files'}
                            </p>
                            <div className="flex items-center gap-2">
                                {FORMAT_INFO.map(({ ext }) => (
                                    <span
                                        key={ext}
                                        className={`px-2.5 py-1 rounded-full text-xs font-medium border ${isDark ? 'bg-white/5 border-white/10 text-white/50' : 'bg-white border-gray-200 text-slate-500 shadow-sm'}`}
                                    >
                                        .{ext.toLowerCase()}
                                    </span>
                                ))}
                                <span
                                    className={`text-xs ${isDark ? 'text-white/25' : 'text-slate-400'}`}
                                >
                                    · 50 MB max
                                </span>
                            </div>
                        </>
                    )}
                </div>
            </div>

            {/* What happens next */}
            {isIdle && uploadedFiles.length === 0 && (
                <div
                    className={`rounded-2xl px-5 py-4 border ${isDark ? 'bg-white/[0.03] border-white/[0.06]' : 'bg-gray-50 border-gray-100'}`}
                >
                    <p
                        className={`text-xs font-semibold uppercase tracking-widest mb-3 ${isDark ? 'text-white/30' : 'text-slate-400'}`}
                    >
                        What happens next
                    </p>
                    <ol className="space-y-2.5">
                        {STEPS.map((step, i) => (
                            <li key={step} className="flex items-center gap-3">
                                <span
                                    className={`w-5 h-5 rounded-full text-xs font-bold flex items-center justify-center flex-shrink-0 border ${isDark ? 'bg-blue-500/15 text-blue-400 border-blue-500/20' : 'bg-blue-100 text-blue-600 border-blue-200'}`}
                                >
                                    {i + 1}
                                </span>
                                <span
                                    className={`text-sm ${isDark ? 'text-white/50' : 'text-slate-500'}`}
                                >
                                    {step}
                                </span>
                            </li>
                        ))}
                    </ol>
                </div>
            )}
        </div>
    );
}
