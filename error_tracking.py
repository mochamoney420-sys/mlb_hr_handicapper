"""Central error tracking system to catch and log all silent failures."""
import os
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class SilentFailureTracker:
    """Tracks all errors and exceptions that might be silently caught."""
    
    def __init__(self, log_dir: str = "data"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self.errors: List[Dict[str, Any]] = []
        self.error_file = self.log_dir / f"silent_failures_{datetime.today().strftime('%Y-%m-%d')}.jsonl"
        
    def log_error(self, category: str, context: str, error: Exception, severity: str = "ERROR"):
        """
        Log an error with context.
        
        Args:
            category: Error category (e.g., 'model_training', 'discord_send', 'data_load')
            context: Description of what was happening
            error: The exception object
            severity: 'ERROR', 'WARNING', 'CRITICAL'
        """
        error_entry = {
            "timestamp": datetime.now().isoformat(),
            "category": category,
            "context": context,
            "error_type": type(error).__name__,
            "error_msg": str(error),
            "severity": severity,
        }
        self.errors.append(error_entry)
        
        # Append to persistent log file
        try:
            with open(self.error_file, 'a') as f:
                f.write(json.dumps(error_entry) + '\n')
        except Exception as e:
            print(f"WARNING: Could not write to error log: {e}")
        
        # Print to console with color coding
        severity_icon = {
            "ERROR": "❌",
            "WARNING": "⚠️ ",
            "CRITICAL": "🚨"
        }.get(severity, "ℹ️ ")
        print(f"{severity_icon} [{category}] {context}: {error}")
    
    def log_warning(self, category: str, context: str, message: str):
        """Log a warning message."""
        warning_entry = {
            "timestamp": datetime.now().isoformat(),
            "category": category,
            "context": context,
            "message": message,
            "severity": "WARNING",
        }
        self.errors.append(warning_entry)
        
        try:
            with open(self.error_file, 'a') as f:
                f.write(json.dumps(warning_entry) + '\n')
        except Exception:
            pass
        
        print(f"⚠️  [{category}] {context}: {message}")
    
    def get_summary(self) -> Dict[str, int]:
        """Get count of errors by severity."""
        summary = {"ERROR": 0, "WARNING": 0, "CRITICAL": 0}
        for err in self.errors:
            sev = err.get("severity", "ERROR")
            summary[sev] += 1
        return summary
    
    def get_critical_errors(self) -> List[Dict]:
        """Get all critical errors found today."""
        return [e for e in self.errors if e.get("severity") == "CRITICAL"]
    
    def write_daily_report(self):
        """Write a daily summary report."""
        if not self.errors:
            return
        
        summary = self.get_summary()
        report_file = self.log_dir / f"error_report_{datetime.today().strftime('%Y-%m-%d')}.txt"
        
        with open(report_file, 'w') as f:
            f.write("=" * 70 + "\n")
            f.write("SILENT FAILURE REPORT\n")
            f.write(f"Generated: {datetime.now().isoformat()}\n")
            f.write("=" * 70 + "\n\n")
            
            f.write(f"Summary:\n")
            f.write(f"  Errors: {summary['ERROR']}\n")
            f.write(f"  Warnings: {summary['WARNING']}\n")
            f.write(f"  Critical: {summary['CRITICAL']}\n\n")
            
            critical = self.get_critical_errors()
            if critical:
                f.write("CRITICAL ISSUES:\n")
                for err in critical:
                    f.write(f"  [{err['category']}] {err['context']}\n")
                    f.write(f"    Error: {err['error_msg']}\n\n")
            
            f.write("\nAll Errors:\n")
            for err in self.errors:
                f.write(f"  {err['timestamp']} [{err['severity']}] {err['category']}: {err['context']}\n")
                if 'error_msg' in err:
                    f.write(f"    {err['error_msg']}\n")
        
        print(f"✅ Error report written: {report_file}")


# Global tracker instance
_tracker: Optional[SilentFailureTracker] = None


def get_tracker() -> SilentFailureTracker:
    """Get or create the global tracker."""
    global _tracker
    if _tracker is None:
        _tracker = SilentFailureTracker()
    return _tracker


def log_error(category: str, context: str, error: Exception, severity: str = "ERROR"):
    """Log an error to the global tracker."""
    get_tracker().log_error(category, context, error, severity)


def log_warning(category: str, context: str, message: str):
    """Log a warning to the global tracker."""
    get_tracker().log_warning(category, context, message)
