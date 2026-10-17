"""
Agent tools for data analysis, cleaning, and code generation.
"""

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class DataAnalysisTools:
    """Tools for data analysis operations."""

    @staticmethod
    def describe_statistics(df: Optional[pd.DataFrame]) -> Dict[str, Any]:
        """Generate comprehensive statistics for the dataset."""
        try:
            if df is None:
                return {"error": "No dataset available"}

            stats = {
                "shape": {"rows": df.shape[0], "columns": df.shape[1]},
                "columns": list(df.columns),
                "dtypes": df.dtypes.astype(str).to_dict(),
                "numeric_summary": df.describe().to_dict(),
                "missing_values": df.isnull().sum().to_dict(),
                "memory_usage": df.memory_usage(deep=True).sum(),
            }
            return stats
        except Exception as e:
            logger.error(f"Error in describe_statistics: {e}")
            return {"error": str(e)}

    @staticmethod
    def correlation_analysis(df: pd.DataFrame) -> Dict[str, Any]:
        """Analyze correlations between numeric columns."""
        try:
            numeric_df = df.select_dtypes(include=[np.number])
            if numeric_df.empty:
                return {"error": "No numeric columns found"}

            corr_matrix = numeric_df.corr()

            # Find strong correlations
            strong_correlations = []
            for i in range(len(corr_matrix.columns)):
                for j in range(i + 1, len(corr_matrix.columns)):
                    corr_value = corr_matrix.iloc[i, j]
                    if abs(corr_value) > 0.7:  # Strong correlation threshold
                        strong_correlations.append(
                            {
                                "column1": corr_matrix.columns[i],
                                "column2": corr_matrix.columns[j],
                                "correlation": round(corr_value, 3),
                            }
                        )

            return {
                "correlation_matrix": corr_matrix.to_dict(),
                "strong_correlations": strong_correlations,
            }
        except Exception as e:
            logger.error(f"Error in correlation_analysis: {e}")
            return {"error": str(e)}

    @staticmethod
    def distribution_analysis(df: pd.DataFrame, column: str) -> Dict[str, Any]:
        """Analyze distribution of a specific column."""
        try:
            if column not in df.columns:
                return {"error": f"Column '{column}' not found"}

            col_data = df[column]

            if pd.api.types.is_numeric_dtype(col_data):
                return {
                    "type": "numeric",
                    "mean": float(col_data.mean()),
                    "median": float(col_data.median()),
                    "std": float(col_data.std()),
                    "min": float(col_data.min()),
                    "max": float(col_data.max()),
                    "quartiles": {
                        "Q1": float(col_data.quantile(0.25)),
                        "Q2": float(col_data.quantile(0.5)),
                        "Q3": float(col_data.quantile(0.75)),
                    },
                    "skewness": float(col_data.skew()),
                    "kurtosis": float(col_data.kurtosis()),
                }
            else:
                value_counts = col_data.value_counts()
                return {
                    "type": "categorical",
                    "unique_values": int(col_data.nunique()),
                    "most_common": value_counts.head(10).to_dict(),
                    "null_count": int(col_data.isnull().sum()),
                }
        except Exception as e:
            logger.error(f"Error in distribution_analysis: {e}")
            return {"error": str(e)}

    @staticmethod
    def missing_values_report(df: pd.DataFrame) -> Dict[str, Any]:
        """Generate detailed missing values report."""
        try:
            missing = df.isnull().sum()
            missing_pct = (missing / len(df)) * 100

            report = []
            for col in df.columns:
                if missing[col] > 0:
                    report.append(
                        {
                            "column": col,
                            "missing_count": int(missing[col]),
                            "missing_percentage": round(float(missing_pct[col]), 2),
                            "dtype": str(df[col].dtype),
                        }
                    )

            return {
                "total_rows": len(df),
                "columns_with_missing": len(report),
                "details": sorted(report, key=lambda x: x["missing_count"], reverse=True),
            }
        except Exception as e:
            logger.error(f"Error in missing_values_report: {e}")
            return {"error": str(e)}


class DataCleaningTools:
    """Tools for data cleaning operations."""

    @staticmethod
    def remove_duplicates(df: pd.DataFrame, subset: Optional[List[str]] = None) -> Dict[str, Any]:
        """Remove duplicate rows."""
        try:
            initial_rows = len(df)
            df_cleaned = df.drop_duplicates(subset=subset)
            removed_rows = initial_rows - len(df_cleaned)

            return {
                "initial_rows": initial_rows,
                "removed_rows": removed_rows,
                "final_rows": len(df_cleaned),
                "data": df_cleaned,
            }
        except Exception as e:
            logger.error(f"Error in remove_duplicates: {e}")
            return {"error": str(e)}

    @staticmethod
    def handle_missing_values(
        df: pd.DataFrame, strategy: str = "auto", columns: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Handle missing values with various strategies."""
        try:
            df_cleaned = df.copy()
            operations: List[str] = []

            target_cols = columns if columns else df_cleaned.columns

            for col in target_cols:
                if col not in df_cleaned.columns:
                    continue
                if df_cleaned[col].isnull().sum() == 0:
                    continue

                if strategy == "auto":
                    # Use assignment (not inplace=True) — pandas 2.x CoW makes
                    # inplace on a column slice a silent no-op.
                    if pd.api.types.is_numeric_dtype(df_cleaned[col]):
                        df_cleaned[col] = df_cleaned[col].fillna(df_cleaned[col].median())
                        operations.append(f"{col}: filled with median")
                    else:
                        mode_series = df_cleaned[col].mode()
                        fill_val = mode_series.iloc[0] if not mode_series.empty else "Unknown"
                        df_cleaned[col] = df_cleaned[col].fillna(fill_val)
                        operations.append(f"{col}: filled with mode")

                elif strategy == "drop":
                    df_cleaned = df_cleaned.dropna(subset=[col])
                    operations.append(f"{col}: rows dropped")

                elif strategy == "forward_fill":
                    df_cleaned[col] = df_cleaned[col].ffill()
                    operations.append(f"{col}: forward filled")

            return {
                "initial_rows": len(df),
                "final_rows": len(df_cleaned),
                "operations": operations,
                "data": df_cleaned,
            }
        except Exception as e:
            logger.error(f"Error in handle_missing_values: {e}")
            return {"error": str(e)}

    @staticmethod
    def detect_outliers(df: pd.DataFrame, column: str, method: str = "iqr") -> Dict[str, Any]:
        """Detect outliers in a numeric column."""
        try:
            if column not in df.columns:
                return {"error": f"Column '{column}' not found"}

            col_data = df[column]
            if not pd.api.types.is_numeric_dtype(col_data):
                return {"error": f"Column '{column}' is not numeric"}

            if method == "iqr":
                Q1 = col_data.quantile(0.25)
                Q3 = col_data.quantile(0.75)
                IQR = Q3 - Q1
                lower_bound = Q1 - 1.5 * IQR
                upper_bound = Q3 + 1.5 * IQR

                outliers = df[(col_data < lower_bound) | (col_data > upper_bound)]

                return {
                    "method": "IQR",
                    "lower_bound": float(lower_bound),
                    "upper_bound": float(upper_bound),
                    "outlier_count": len(outliers),
                    "outlier_percentage": round(len(outliers) / len(df) * 100, 2),
                    "outlier_indices": outliers.index.tolist()[:50],  # Limit to 50
                }

            elif method == "zscore":
                mean = col_data.mean()
                std = col_data.std()
                z_scores = np.abs((col_data - mean) / std)
                outliers = df[z_scores > 3]

                return {
                    "method": "Z-Score",
                    "threshold": 3,
                    "outlier_count": len(outliers),
                    "outlier_percentage": round(len(outliers) / len(df) * 100, 2),
                    "outlier_indices": outliers.index.tolist()[:50],
                }

            return {"error": f"Unknown method: {method}"}
        except Exception as e:
            logger.error(f"Error in detect_outliers: {e}")
            return {"error": str(e)}

    @staticmethod
    def generate_cleaning_code(operations: List[Dict[str, Any]]) -> str:
        """Generate Python code for data cleaning operations."""
        code_lines = [
            "import pandas as pd",
            "import numpy as np",
            "",
            "# Load your data",
            "df = pd.read_csv('your_file.csv')",
            "",
            "# Data cleaning operations",
        ]

        for op in operations:
            if op["type"] == "remove_duplicates":
                code_lines.append("df = df.drop_duplicates()")

            elif op["type"] == "fill_missing":
                col = op.get("column")
                strategy = op.get("strategy", "median")
                if strategy == "median":
                    code_lines.append(f"df['{col}'].fillna(df['{col}'].median(), inplace=True)")
                elif strategy == "mode":
                    code_lines.append(f"df['{col}'].fillna(df['{col}'].mode()[0], inplace=True)")
                elif strategy == "drop":
                    code_lines.append(f"df.dropna(subset=['{col}'], inplace=True)")

            elif op["type"] == "remove_outliers":
                col = op.get("column")
                code_lines.append(f"# Remove outliers from {col}")
                code_lines.append(f"Q1 = df['{col}'].quantile(0.25)")
                code_lines.append(f"Q3 = df['{col}'].quantile(0.75)")
                code_lines.append("IQR = Q3 - Q1")
                code_lines.append(
                    f"df = df[~((df['{col}'] < (Q1 - 1.5 * IQR)) | (df['{col}'] > (Q3 + 1.5 * IQR)))]"
                )

        code_lines.extend(
            [
                "",
                "# Save cleaned data",
                "df.to_csv('cleaned_data.csv', index=False)",
                "print(f'Cleaned data saved. Shape: {df.shape}')",
            ]
        )

        return "\n".join(code_lines)


class CodeGenerationTools:
    """Tools for generating analysis and visualization code."""

    @staticmethod
    def generate_visualization_code(
        chart_type: str, columns: List[str], title: Optional[str] = None
    ) -> str:
        """Generate matplotlib/seaborn visualization code."""
        code_lines = [
            "import matplotlib.pyplot as plt",
            "import seaborn as sns",
            "",
            "# Set style",
            "sns.set_style('whitegrid')",
            "",
        ]

        if chart_type == "histogram":
            col = columns[0]
            code_lines.extend(
                [
                    "plt.figure(figsize=(10, 6))",
                    f"plt.hist(df['{col}'], bins=30, edgecolor='black')",
                    f"plt.xlabel('{col}')",
                    "plt.ylabel('Frequency')",
                    f"plt.title('{title or f'Distribution of {col}'}')",
                    "plt.show()",
                ]
            )

        elif chart_type == "scatter":
            x, y = columns[0], columns[1]
            code_lines.extend(
                [
                    "plt.figure(figsize=(10, 6))",
                    f"plt.scatter(df['{x}'], df['{y}'], alpha=0.5)",
                    f"plt.xlabel('{x}')",
                    f"plt.ylabel('{y}')",
                    f"plt.title('{title or f'{x} vs {y}'}')",
                    "plt.show()",
                ]
            )

        elif chart_type == "correlation_heatmap":
            code_lines.extend(
                [
                    "plt.figure(figsize=(12, 8))",
                    "correlation_matrix = df.corr()",
                    "sns.heatmap(correlation_matrix, annot=True, cmap='coolwarm', center=0)",
                    f"plt.title('{title or 'Correlation Heatmap'}')",
                    "plt.show()",
                ]
            )

        return "\n".join(code_lines)
