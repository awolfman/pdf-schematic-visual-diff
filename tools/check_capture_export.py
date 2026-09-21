#!/usr/bin/env python3
"""Unit tests for check_capture_export.py.

Run:
    python3 -m unittest -v test_check_capture_export
"""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from check_capture_export import (
    parse_log, check_export, main as cli_main,
    EXIT_PRECHECK_PASSED, EXIT_REFUSED, EXIT_IO_ERROR,
    REQUIRED_FILES,
)


# ---------------------------------------------------------------------------
# Sample logs
# ---------------------------------------------------------------------------

SUCCESS_LOG = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "\n"
    '#1 WARNING(ORCAP-36006): Part Name "LONG_PART_NAME" is renamed '
    'to "SHORT_PART_NAME".\n'
    "INFO(ORCAP-36080): Scanning netlist files ...\n"
    "\n"
    "Loading... Z:\\home\\ea\\work\\Test/pstchip.dat\n"
    "\n"
    "Loading... Z:\\home\\ea\\work\\Test/pstxnet.dat\n"
    "packaging the design view...\n"
)

SUCCESS_LOG_MANY = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    + "".join(
        f'#{i} WARNING(ORCAP-36006): Part Name "L{i}" is renamed '
        f'to "S{i}".\n'
        for i in range(1, 6)
    )
    + "INFO(ORCAP-36080): Scanning netlist files ...\n"
)

ERROR_LOG = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:03:45 }\n"
    "\n"
    '#1 ERROR(ORCAP-36002): Property "PCB Footprint" missing from '
    "instance F1: SCHEMATIC1, 01_INPUT_PWR (155.00, 45.00).\n"
    "#2 ERROR(ORCAP-36018): Aborting Netlisting... Please correct the "
    "above errors and retry.\n"
)

UNKNOWN_ERROR_CODE_LOG = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:03:45 }\n"
    "#1 ERROR(ORCAP-99999): Something else went wrong.\n"
)

UNKNOWN_WARNING_CODE_LOG = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "#1 WARNING(ORCAP-40000): Strange but not fatal.\n"
    "#2 WARNING(ORCAP-36006): renamed LONG to SHORT.\n"
    "INFO(ORCAP-36080): scanning.\n"
)

FATAL_LOG = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "#1 FATAL(ORCAP-00000): boom\n"
)

ERROR_ORCAP_36006_LOG = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "#1 ERROR(ORCAP-36006): a hard failure, not a rename\n"
)

WORD_ERROR_IN_TEXT_LOG = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "INFO(ORCAP-36080): scanning, report any error in the log\n"
    "#1 WARNING(ORCAP-36006): renamed\n"
)

HEADER_ONLY_LOG = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
)

UNRECOGNISED_LOG = (
    "just some text\n"
    "no header here\n"
)

UNSUPPORTED_NUMBERED_LOG = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "#1 ALERT(ORCAP-12345): something new\n"
    "INFO(ORCAP-36080): ok\n"
)

UNSUPPORTED_UNNUMBERED_LOG = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "ALERT(ORCAP-12345): something new\n"
    "INFO(ORCAP-36080): ok\n"
)

PSTWRITER_IN_PLAIN_TEXT = (
    "Some text mentioning PSTWRITER here\n"
    "INFO(ORCAP-36080): scanning.\n"
)

PATH_WITH_ERROR_DIR = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "Loading... Z:\\ERROR\\pstchip.dat\n"
    "INFO(ORCAP-36080): scanning.\n"
)

MALFORMED_ERROR_COLON = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "INFO(ORCAP-36080): scanning.\n"
    "ERROR: export failed\n"
)

MALFORMED_WARNING_NO_PARENS = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "INFO(ORCAP-36080): scanning.\n"
    "WARNING malformed diagnostic\n"
)

MALFORMED_ALERT_SPACE = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "INFO(ORCAP-36080): scanning.\n"
    "ALERT ORCAP-12345: something went wrong\n"
)

MALFORMED_ALERT_PARENS = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "INFO(ORCAP-36080): scanning.\n"
    "ALERT(ORCAP-12345): something went wrong\n"
)

MALFORMED_NUMBERED_ERROR = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "INFO(ORCAP-36080): scanning.\n"
    "#1 ERROR ORCAP-36002: missing footprint\n"
)

SINGLE_TOKEN_ORCAP_LIKE = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "INFO(ORCAP-36080): scanning.\n"
    "ALERTORCAP-12345.txt\n"
)

ORCAP_CODE_WITH_SUFFIX = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "INFO(ORCAP-36080): scanning.\n"
    "ALERT ORCAP-12345XYZ\n"
)

NON_DIAGNOSTIC_LINES = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "INFO(ORCAP-36080): scanning.\n"
    "Loading... Z:\\ERROR\\pstchip.dat\n"
    "The operator reported an ERROR in the log.\n"
    "packaging the design view...\n"
    "# ordinary comment\n"
    "ERROR_REPORT.txt\n"
)

# ERROR line whose text contains the word PSTWRITER.  There is no real
# PSTWRITER header in the file.  The subsequent INFO must still be
# parsed; the log therefore contains two diagnostic messages.
ERROR_WITH_PSTWRITER_TEXT_NO_HEADER = (
    "ERROR(ORCAP-36002): PSTWRITER export failed\n"
    "INFO(ORCAP-36080): scanning.\n"
)

# Very long sequence number, followed by a valid INFO line.  The long
# number is recorded as an unsupported diagnostic; the following INFO
# is parsed normally.
LONG_SEQ_LOG = (
    "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "#" + ("9" * 100) + " WARNING(ORCAP-36006): renamed\n"
    "INFO(ORCAP-36080): scanning.\n"
)

INDENTED_HEADER_LOG = (
    "   { Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
    "#7 WARNING(ORCAP-36006): renamed\n"
    "INFO(ORCAP-36080): scanning\n"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(path: str, content, encoding: str = "utf-8") -> None:
    with open(path, "wb") as f:
        if isinstance(content, str):
            f.write(content.encode(encoding))
        else:
            f.write(content)


def _make_dat_set(directory: str, size: int = 32) -> None:
    for name in ("pstchip.dat", "pstxprt.dat", "pstxnet.dat"):
        _write(os.path.join(directory, name), b"x" * size)


def _capture_stdout(callable_, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = callable_(*args, **kwargs)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# Log parser
# ---------------------------------------------------------------------------

class ParseLogTest(unittest.TestCase):

    def test_success_log(self):
        p = parse_log(SUCCESS_LOG)
        self.assertTrue(p["header_found"])
        self.assertEqual(
            p["header_line"],
            "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }")
        levels = [m["level"] for m in p["messages"]]
        self.assertEqual(levels, ["WARNING", "INFO"])
        self.assertEqual(p["orcap_36006_count"], 1)
        loading = [l for l in p["other_lines"]
                   if l["raw"].startswith("Loading...")]
        self.assertGreaterEqual(len(loading), 2)
        self.assertTrue(any(l["raw"].startswith("packaging the design")
                            for l in p["other_lines"]))

    def test_many_warnings(self):
        p = parse_log(SUCCESS_LOG_MANY)
        self.assertEqual(p["orcap_36006_count"], 5)
        self.assertEqual(p["counts"].get("WARNING"), 5)
        self.assertNotIn("ERROR", p["counts"])

    def test_error_log(self):
        p = parse_log(ERROR_LOG)
        self.assertEqual([m["code"] for m in p["messages"]],
                         ["ORCAP-36002", "ORCAP-36018"])
        self.assertEqual(p["counts"].get("ERROR"), 2)
        self.assertIn("F1", p["messages"][0]["text"])
        self.assertIn("PCB Footprint", p["messages"][0]["text"])

    def test_unknown_error_code_preserved(self):
        p = parse_log(UNKNOWN_ERROR_CODE_LOG)
        self.assertEqual(len(p["messages"]), 1)
        self.assertEqual(p["messages"][0]["level"], "ERROR")
        self.assertEqual(p["messages"][0]["code"], "ORCAP-99999")

    def test_unknown_warning_code_preserved(self):
        p = parse_log(UNKNOWN_WARNING_CODE_LOG)
        codes = [m["code"] for m in p["messages"]]
        self.assertIn("ORCAP-40000", codes)
        self.assertIn("ORCAP-36006", codes)
        self.assertEqual(p["orcap_36006_count"], 1)

    def test_fatal_blocks(self):
        p = parse_log(FATAL_LOG)
        self.assertEqual(len(p["messages"]), 1)
        self.assertEqual(p["messages"][0]["level"], "FATAL")

    def test_error_orcap_36006_not_in_allowed_counter(self):
        p = parse_log(ERROR_ORCAP_36006_LOG)
        self.assertEqual(p["counts"].get("ERROR"), 1)
        self.assertEqual(p["orcap_36006_count"], 0)

    def test_word_error_inside_text_is_not_error(self):
        p = parse_log(WORD_ERROR_IN_TEXT_LOG)
        self.assertFalse(any(m["level"] == "ERROR"
                             for m in p["messages"]))

    def test_crlf_and_lf_yield_same_result(self):
        lf = SUCCESS_LOG
        crlf = SUCCESS_LOG.replace("\n", "\r\n")
        self.assertEqual(parse_log(lf), parse_log(crlf))

    def test_no_final_newline(self):
        text = SUCCESS_LOG.rstrip("\n")
        p = parse_log(text)
        self.assertTrue(p["header_found"])
        self.assertEqual(p["orcap_36006_count"], 1)

    def test_header_only(self):
        p = parse_log(HEADER_ONLY_LOG)
        self.assertTrue(p["header_found"])
        self.assertEqual(p["messages"], [])
        self.assertEqual(p["unknown_diagnostic_lines"], [])
        self.assertEqual(p["line_count"], 1)

    def test_unrecognised(self):
        p = parse_log(UNRECOGNISED_LOG)
        self.assertFalse(p["header_found"])

    def test_header_requires_braces_and_using(self):
        p = parse_log(PSTWRITER_IN_PLAIN_TEXT)
        self.assertFalse(p["header_found"])
        self.assertTrue(any("PSTWRITER" in l["raw"]
                            for l in p["other_lines"]))

    def test_error_with_pstwriter_text_is_not_header(self):
        # The ERROR line contains the word PSTWRITER, but there is no
        # real header.  Both diagnostics must still be parsed.
        p = parse_log(ERROR_WITH_PSTWRITER_TEXT_NO_HEADER)
        self.assertFalse(p["header_found"])
        self.assertEqual([m["level"] for m in p["messages"]],
                         ["ERROR", "INFO"])
        self.assertEqual(len(p["messages"]), 2)
        # PSTWRITER text is preserved in the ERROR message body.
        self.assertIn("PSTWRITER", p["messages"][0]["text"])

    def test_header_indent_and_seq_preserved(self):
        p = parse_log(INDENTED_HEADER_LOG)
        self.assertTrue(p["header_found"])
        self.assertTrue(p["header_line"].startswith("   {"))
        self.assertEqual(len(p["messages"]), 2)
        self.assertEqual(p["messages"][0]["seq"], 7)
        self.assertIsNone(p["messages"][1]["seq"])

    def test_numbered_unsupported_level_goes_to_unknown(self):
        p = parse_log(UNSUPPORTED_NUMBERED_LOG)
        self.assertEqual(len(p["unknown_diagnostic_lines"]), 1)
        self.assertIn("ALERT",
                      p["unknown_diagnostic_lines"][0]["raw"])

    def test_unnumbered_unsupported_level_goes_to_unknown(self):
        p = parse_log(UNSUPPORTED_UNNUMBERED_LOG)
        self.assertEqual(len(p["unknown_diagnostic_lines"]), 1)
        self.assertIn("ALERT",
                      p["unknown_diagnostic_lines"][0]["raw"])

    def test_plain_malformed_error_colon(self):
        p = parse_log(MALFORMED_ERROR_COLON)
        self.assertEqual(len(p["unknown_diagnostic_lines"]), 1)
        self.assertIn("ERROR",
                      p["unknown_diagnostic_lines"][0]["raw"])

    def test_plain_malformed_warning_no_parens(self):
        p = parse_log(MALFORMED_WARNING_NO_PARENS)
        self.assertEqual(len(p["unknown_diagnostic_lines"]), 1)

    def test_plain_malformed_alert_space(self):
        p = parse_log(MALFORMED_ALERT_SPACE)
        self.assertEqual(len(p["unknown_diagnostic_lines"]), 1)
        self.assertIn("ALERT",
                      p["unknown_diagnostic_lines"][0]["raw"])

    def test_plain_malformed_alert_parens(self):
        p = parse_log(MALFORMED_ALERT_PARENS)
        self.assertEqual(len(p["unknown_diagnostic_lines"]), 1)

    def test_plain_malformed_numbered_error(self):
        p = parse_log(MALFORMED_NUMBERED_ERROR)
        self.assertEqual(len(p["unknown_diagnostic_lines"]), 1)
        self.assertIn("#1", p["unknown_diagnostic_lines"][0]["raw"])

    def test_single_token_orcap_like_not_blocked(self):
        p = parse_log(SINGLE_TOKEN_ORCAP_LIKE)
        self.assertEqual(p["unknown_diagnostic_lines"], [])
        self.assertTrue(any("ALERTORCAP-12345.txt" in l["raw"]
                            for l in p["other_lines"]))

    def test_orcap_code_with_suffix_not_blocked(self):
        p = parse_log(ORCAP_CODE_WITH_SUFFIX)
        self.assertEqual(p["unknown_diagnostic_lines"], [])
        self.assertTrue(any("ORCAP-12345XYZ" in l["raw"]
                            for l in p["other_lines"]))

    def test_path_with_error_word_is_not_unknown(self):
        p = parse_log(PATH_WITH_ERROR_DIR)
        self.assertEqual(p["unknown_diagnostic_lines"], [])
        self.assertTrue(any("ERROR" in l["raw"]
                            for l in p["other_lines"]))

    def test_non_diagnostic_lines_are_not_unknown(self):
        p = parse_log(NON_DIAGNOSTIC_LINES)
        self.assertEqual(p["unknown_diagnostic_lines"], [])
        raws = [l["raw"] for l in p["other_lines"]]
        self.assertTrue(any("ERROR_REPORT.txt" in r for r in raws))
        self.assertTrue(any("# ordinary comment" in r for r in raws))
        self.assertTrue(any("packaging the design view" in r
                            for r in raws))

    def test_raw_lines_are_preserved(self):
        p = parse_log(SUCCESS_LOG)
        source_lines = set(SUCCESS_LOG.splitlines())
        for m in p["messages"]:
            self.assertIn(m["raw"], source_lines)
        for l in p["other_lines"]:
            self.assertIn(l["raw"], source_lines)

    def test_text_field_is_trimmed_raw_is_not(self):
        text = (
            "{ Using PSTWRITER 17.2.0 d001Sep-21-2026 at 09:01:08 }\n"
            "INFO(ORCAP-36080):   spaced   body   \n"
        )
        p = parse_log(text)
        self.assertEqual(len(p["messages"]), 1)
        m = p["messages"][0]
        self.assertEqual(m["text"], "spaced   body")
        self.assertIn("spaced   body   ", m["raw"])

    def test_line_count_ignores_phantom_trailing_line(self):
        p1 = parse_log("a\nb\n")
        p2 = parse_log("a\nb")
        self.assertEqual(p1["line_count"], 2)
        self.assertEqual(p2["line_count"], 2)

    def test_whitespace_only_lines_are_blank(self):
        p = parse_log("a\n   \n\t\nb\n")
        self.assertEqual(p["line_count"], 4)
        self.assertEqual(p["blank_line_count"], 2)
        self.assertEqual([l["raw"] for l in p["other_lines"]],
                         ["a", "b"])

    def test_long_sequence_number_blocks_without_crash(self):
        # The line with the long sequence number is recorded as
        # unsupported; the following INFO is still parsed.
        p = parse_log(LONG_SEQ_LOG)
        self.assertEqual(len(p["unknown_diagnostic_lines"]), 1)
        entry = p["unknown_diagnostic_lines"][0]
        self.assertIn("sequence number", entry["reason"])
        # The next line (INFO) is parsed normally.
        self.assertEqual([m["level"] for m in p["messages"]], ["INFO"])


# ---------------------------------------------------------------------------
# check_export on temporary directories
# ---------------------------------------------------------------------------

class CheckExportTest(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ccx_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _base(self, log_text: str = SUCCESS_LOG,
              dat_size: int = 32,
              encoding: str = "utf-8",
              write_log: bool = True) -> None:
        if write_log:
            _write(os.path.join(self.tmp, "netlist.log"), log_text,
                   encoding=encoding)
        _make_dat_set(self.tmp, size=dat_size)

    def test_success(self):
        self._base()
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "precheck_passed")
        self.assertEqual(r["exit_code"], 0)
        self.assertTrue(r["precheck_passed"])
        self.assertEqual(r["block_reasons"], [])
        self.assertEqual(r["io_errors"], [])
        self.assertEqual(r["runtime_errors"], [])
        self.assertEqual(r["log"]["orcap_36006_count"], 1)
        for f in r["files"]:
            self.assertEqual(f["state"], "present")

    def test_error_log_blocks(self):
        self._base(log_text=ERROR_LOG)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")
        self.assertEqual(r["exit_code"], 1)

    def test_unknown_error_code_blocks(self):
        self._base(log_text=UNKNOWN_ERROR_CODE_LOG)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")

    def test_fatal_blocks(self):
        self._base(log_text=FATAL_LOG)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")

    def test_unknown_warning_does_not_block(self):
        self._base(log_text=UNKNOWN_WARNING_CODE_LOG)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "precheck_passed")

    def test_word_error_does_not_block(self):
        self._base(log_text=WORD_ERROR_IN_TEXT_LOG)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "precheck_passed")

    def test_path_with_error_dir_does_not_block(self):
        self._base(log_text=PATH_WITH_ERROR_DIR)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "precheck_passed")

    def test_pstwriter_mention_without_header_blocks(self):
        self._base(log_text=PSTWRITER_IN_PLAIN_TEXT)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")
        self.assertFalse(r["log"]["header_found"])

    def test_error_with_pstwriter_text_no_header_refused(self):
        # Same log as test_error_with_pstwriter_text_is_not_header.
        # Result: no header, two diagnostics (ERROR and INFO), refused.
        self._base(log_text=ERROR_WITH_PSTWRITER_TEXT_NO_HEADER)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")
        self.assertEqual(r["exit_code"], 1)
        self.assertFalse(r["log"]["header_found"])
        self.assertEqual([m["level"] for m in r["log"]["messages"]],
                         ["ERROR", "INFO"])
        # PSTWRITER text preserved inside the ERROR body.
        self.assertIn("PSTWRITER",
                      r["log"]["messages"][0]["text"])
        # Both reasons are present.
        reasons = " ".join(r["block_reasons"])
        self.assertIn("no PSTWRITER header", reasons)
        self.assertIn("ERROR/FATAL", reasons)

    def test_malformed_error_colon_blocks(self):
        self._base(log_text=MALFORMED_ERROR_COLON)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")
        self.assertTrue(r["log"]["header_found"])
        self.assertEqual(len(r["log"]["unknown_diagnostic_lines"]), 1)

    def test_malformed_warning_no_parens_blocks(self):
        self._base(log_text=MALFORMED_WARNING_NO_PARENS)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")

    def test_malformed_alert_space_blocks(self):
        self._base(log_text=MALFORMED_ALERT_SPACE)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")

    def test_malformed_alert_parens_blocks(self):
        self._base(log_text=MALFORMED_ALERT_PARENS)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")

    def test_malformed_numbered_error_blocks(self):
        self._base(log_text=MALFORMED_NUMBERED_ERROR)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")

    def test_single_token_orcap_like_does_not_block(self):
        self._base(log_text=SINGLE_TOKEN_ORCAP_LIKE)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "precheck_passed")

    def test_orcap_code_with_suffix_does_not_block(self):
        self._base(log_text=ORCAP_CODE_WITH_SUFFIX)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "precheck_passed")

    def test_non_diagnostic_lines_pass(self):
        self._base(log_text=NON_DIAGNOSTIC_LINES)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "precheck_passed")
        self.assertEqual(r["log"]["unknown_diagnostic_lines"], [])

    def test_long_seq_blocks(self):
        self._base(log_text=LONG_SEQ_LOG)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")
        self.assertEqual(len(r["log"]["unknown_diagnostic_lines"]), 1)
        # The INFO line after the bad one is still parsed.
        self.assertEqual([m["level"] for m in r["log"]["messages"]],
                         ["INFO"])

    def test_missing_log(self):
        self._base(write_log=False)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")
        self.assertTrue(any("netlist.log" in x
                            for x in r["block_reasons"]))

    def test_empty_log(self):
        _write(os.path.join(self.tmp, "netlist.log"), b"")
        _make_dat_set(self.tmp)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")
        self.assertTrue(any("empty file: netlist.log" in x
                            for x in r["block_reasons"]))

    def test_unrecognised_log(self):
        self._base(log_text=UNRECOGNISED_LOG)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")

    def test_header_only_log(self):
        self._base(log_text=HEADER_ONLY_LOG)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")
        self.assertTrue(any("no diagnostic messages" in x
                            for x in r["block_reasons"]))

    def test_missing_dat(self):
        _write(os.path.join(self.tmp, "netlist.log"), SUCCESS_LOG)
        _write(os.path.join(self.tmp, "pstchip.dat"), b"x" * 32)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")
        reasons = " ".join(r["block_reasons"])
        self.assertIn("pstxprt.dat", reasons)
        self.assertIn("pstxnet.dat", reasons)

    def test_empty_dat(self):
        _write(os.path.join(self.tmp, "netlist.log"), SUCCESS_LOG)
        _write(os.path.join(self.tmp, "pstchip.dat"), b"")
        _write(os.path.join(self.tmp, "pstxprt.dat"), b"x" * 32)
        _write(os.path.join(self.tmp, "pstxnet.dat"), b"x" * 32)
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")
        self.assertTrue(any("empty file: pstchip.dat" in x
                            for x in r["block_reasons"]))

    def test_directory_instead_of_dat(self):
        self._base()
        os.unlink(os.path.join(self.tmp, "pstchip.dat"))
        os.mkdir(os.path.join(self.tmp, "pstchip.dat"))
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "refused")
        self.assertTrue(any("not a regular file: pstchip.dat" in x
                            for x in r["block_reasons"]))

    def test_utf8_bom(self):
        self._base(log_text=SUCCESS_LOG, encoding="utf-8-sig")
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "precheck_passed")

    def test_unknown_encoding_files_not_absent(self):
        # Full valid input set, but bad encoding.  Because the probe
        # never runs, files must be marked "not_checked", not "absent".
        self._base()
        r = check_export(self.tmp, "definitely-not-a-codec")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["exit_code"], 2)
        for f in r["files"]:
            self.assertEqual(f["state"], "not_checked")
            self.assertIsNone(f["size"])
        self.assertTrue(any(e["type"] == "encoding_not_text"
                            for e in r["runtime_errors"]))

    def test_non_text_codec_returns_error(self):
        self._base()
        r = check_export(self.tmp, "base64")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["exit_code"], 2)
        for f in r["files"]:
            self.assertEqual(f["state"], "not_checked")

    def test_bad_decode_returns_error_and_keeps_files(self):
        self._base()
        _write(os.path.join(self.tmp, "netlist.log"),
               b"{ Using PSTWRITER \xff\xfe }")
        r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["exit_code"], 2)
        self.assertEqual(len(r["files"]), len(REQUIRED_FILES))
        for f in r["files"]:
            self.assertEqual(f["state"], "present")
        self.assertTrue(any(e["type"] == "log_decode_error"
                            for e in r["runtime_errors"]))

    def test_input_dir_missing_files_not_absent(self):
        missing = os.path.join(self.tmp, "no-such-dir")
        r = check_export(missing, "utf-8-sig")
        self.assertEqual(r["status"], "error")
        for f in r["files"]:
            self.assertEqual(f["state"], "not_checked")

    def test_input_dir_is_a_file(self):
        target = os.path.join(self.tmp, "just-a-file")
        _write(target, b"content")
        r = check_export(target, "utf-8-sig")
        self.assertEqual(r["status"], "error")
        self.assertTrue(any(e["type"] == "input_dir_not_directory"
                            for e in r["runtime_errors"]))
        for f in r["files"]:
            self.assertEqual(f["state"], "not_checked")

    def test_input_files_unchanged(self):
        self._base()
        before: dict[str, bytes] = {}
        for name in REQUIRED_FILES:
            p = os.path.join(self.tmp, name)
            with open(p, "rb") as f:
                before[name] = f.read()
        check_export(self.tmp, "utf-8-sig")
        for name in REQUIRED_FILES:
            p = os.path.join(self.tmp, name)
            with open(p, "rb") as f:
                self.assertEqual(f.read(), before[name])

    def test_repeat_check_is_deterministic(self):
        self._base(log_text=ERROR_LOG)
        r1 = check_export(self.tmp, "utf-8-sig")
        r2 = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r1, r2)

    def test_error_log_plus_io_error_is_exit_2(self):
        # Both an ERROR in the log and a read failure on a DAT.
        self._base(log_text=ERROR_LOG)
        real_open = builtins.open

        def fake_open(path, *args, **kwargs):
            try:
                p = os.fspath(path)
            except TypeError:
                return real_open(path, *args, **kwargs)
            if str(p).endswith("pstchip.dat"):
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=fake_open):
            r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["exit_code"], 2)
        self.assertTrue(any("ERROR" in x for x in r["block_reasons"]))
        self.assertTrue(r["io_errors"])

    def test_stat_permission_error_is_io_not_absent(self):
        self._base()
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            try:
                p = os.fspath(path)
            except TypeError:
                return real_stat(path, *args, **kwargs)
            if str(p).endswith("pstchip.dat"):
                raise PermissionError(13, "Permission denied")
            return real_stat(path, *args, **kwargs)

        with mock.patch("os.stat", side_effect=fake_stat):
            r = check_export(self.tmp, "utf-8-sig")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["exit_code"], 2)
        pc = next(f for f in r["files"] if f["name"] == "pstchip.dat")
        self.assertEqual(pc["state"], "unknown")
        self.assertIsNotNone(pc["probe_error"])
        self.assertTrue(r["io_errors"])

    def test_access_refused_per_file(self):
        """Every mandatory file, including netlist.log, must give exit 2
        and state='unknown' when stat() refuses access."""
        for name in REQUIRED_FILES:
            with self.subTest(name=name):
                tmp = tempfile.mkdtemp(prefix="ccx_acc_")
                try:
                    _write(os.path.join(tmp, "netlist.log"), SUCCESS_LOG)
                    _make_dat_set(tmp)
                    real_stat = os.stat

                    def fake_stat(path, *args, _target=name, **kwargs):
                        try:
                            p = os.fspath(path)
                        except TypeError:
                            return real_stat(path, *args, **kwargs)
                        if str(p).endswith(_target):
                            raise PermissionError(13, "Permission denied")
                        return real_stat(path, *args, **kwargs)

                    with mock.patch("os.stat", side_effect=fake_stat):
                        r = check_export(tmp, "utf-8-sig")
                    self.assertEqual(r["status"], "error")
                    self.assertEqual(r["exit_code"], 2)
                    target = next(f for f in r["files"]
                                  if f["name"] == name)
                    self.assertEqual(target["state"], "unknown")
                    self.assertIsNotNone(target["probe_error"])
                    self.assertTrue(r["io_errors"])
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)

    def test_open_permission_error_per_file(self):
        """Every mandatory file, including netlist.log, must give exit 2
        when open() refuses access.  State must remain 'present'
        because the initial stat() succeeded."""
        for name in REQUIRED_FILES:
            with self.subTest(name=name):
                tmp = tempfile.mkdtemp(prefix="ccx_open_")
                try:
                    _write(os.path.join(tmp, "netlist.log"), SUCCESS_LOG)
                    _make_dat_set(tmp)
                    real_open = builtins.open

                    def fake_open(path, *args, _target=name, **kwargs):
                        try:
                            p = os.fspath(path)
                        except TypeError:
                            return real_open(path, *args, **kwargs)
                        if str(p).endswith(_target):
                            raise PermissionError(13, "Permission denied")
                        return real_open(path, *args, **kwargs)

                    with mock.patch("builtins.open",
                                    side_effect=fake_open):
                        r = check_export(tmp, "utf-8-sig")
                    self.assertEqual(r["status"], "error")
                    self.assertEqual(r["exit_code"], 2)
                    target = next(f for f in r["files"]
                                  if f["name"] == name)
                    # stat() succeeded before open() failed.
                    self.assertEqual(target["state"], "present")
                    self.assertTrue(target["is_regular_file"])
                    self.assertFalse(target["readable"])
                    self.assertIsNotNone(target["probe_error"])
                    self.assertTrue(r["io_errors"])
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class CliTest(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ccx_cli_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _base(self, log_text: str = SUCCESS_LOG) -> None:
        _write(os.path.join(self.tmp, "netlist.log"), log_text)
        _make_dat_set(self.tmp)

    def test_cli_success_exit_0(self):
        self._base()
        rc, out = _capture_stdout(cli_main, ["--input-dir", self.tmp])
        self.assertEqual(rc, EXIT_PRECHECK_PASSED)
        self.assertIn("PRECHECK PASSED", out)

    def test_cli_error_log_exit_1(self):
        self._base(log_text=ERROR_LOG)
        rc, _ = _capture_stdout(cli_main, ["--input-dir", self.tmp])
        self.assertEqual(rc, EXIT_REFUSED)

    def test_cli_missing_file_exit_1(self):
        _write(os.path.join(self.tmp, "netlist.log"), SUCCESS_LOG)
        _write(os.path.join(self.tmp, "pstchip.dat"), b"x" * 32)
        _write(os.path.join(self.tmp, "pstxnet.dat"), b"x" * 32)
        rc, _ = _capture_stdout(cli_main, ["--input-dir", self.tmp])
        self.assertEqual(rc, EXIT_REFUSED)

    def test_cli_missing_dir_exit_2(self):
        missing = os.path.join(self.tmp, "does-not-exist")
        rc, _ = _capture_stdout(cli_main, ["--input-dir", missing])
        self.assertEqual(rc, EXIT_IO_ERROR)

    def test_cli_unknown_encoding_exit_2(self):
        self._base()
        rc, _ = _capture_stdout(cli_main, [
            "--input-dir", self.tmp,
            "--encoding", "definitely-not-codec"])
        self.assertEqual(rc, EXIT_IO_ERROR)

    def test_cli_non_text_codec_exit_2(self):
        self._base()
        rc, _ = _capture_stdout(cli_main, [
            "--input-dir", self.tmp, "--encoding", "base64"])
        self.assertEqual(rc, EXIT_IO_ERROR)

    def test_cli_bad_decoding_exit_2(self):
        _write(os.path.join(self.tmp, "netlist.log"),
               b"{ Using PSTWRITER \xff\xfe }")
        _make_dat_set(self.tmp)
        rc, _ = _capture_stdout(cli_main, ["--input-dir", self.tmp])
        self.assertEqual(rc, EXIT_IO_ERROR)

    def test_cli_json_success(self):
        self._base()
        rc, out = _capture_stdout(cli_main,
                                  ["--input-dir", self.tmp, "--json"])
        self.assertEqual(rc, EXIT_PRECHECK_PASSED)
        parsed = json.loads(out)
        self.assertEqual(parsed["status"], "precheck_passed")
        self.assertEqual(parsed["exit_code"], 0)
        self.assertTrue(parsed["precheck_passed"])

    def test_cli_json_refused(self):
        self._base(log_text=ERROR_LOG)
        rc, out = _capture_stdout(cli_main,
                                  ["--input-dir", self.tmp, "--json"])
        self.assertEqual(rc, EXIT_REFUSED)
        parsed = json.loads(out)
        self.assertEqual(parsed["status"], "refused")
        self.assertEqual(parsed["exit_code"], 1)
        self.assertTrue(parsed["block_reasons"])

    def test_cli_json_error_unknown_encoding(self):
        self._base()
        rc, out = _capture_stdout(cli_main, [
            "--input-dir", self.tmp, "--json",
            "--encoding", "definitely-not-codec"])
        self.assertEqual(rc, EXIT_IO_ERROR)
        parsed = json.loads(out)
        self.assertEqual(parsed["status"], "error")
        self.assertEqual(parsed["exit_code"], 2)
        for f in parsed["files"]:
            self.assertEqual(f["state"], "not_checked")

    def test_cli_json_error_non_text_codec(self):
        self._base()
        rc, out = _capture_stdout(cli_main, [
            "--input-dir", self.tmp, "--json", "--encoding", "base64"])
        self.assertEqual(rc, EXIT_IO_ERROR)
        parsed = json.loads(out)
        self.assertEqual(parsed["status"], "error")
        self.assertTrue(any(e["type"] == "encoding_not_text"
                            for e in parsed["runtime_errors"]))

    def test_cli_json_error_decode(self):
        _write(os.path.join(self.tmp, "netlist.log"),
               b"{ Using PSTWRITER \xff\xfe }")
        _make_dat_set(self.tmp)
        rc, out = _capture_stdout(cli_main,
                                  ["--input-dir", self.tmp, "--json"])
        self.assertEqual(rc, EXIT_IO_ERROR)
        parsed = json.loads(out)
        self.assertEqual(parsed["status"], "error")
        self.assertTrue(any(e["type"] == "log_decode_error"
                            for e in parsed["runtime_errors"]))
        for f in parsed["files"]:
            self.assertEqual(f["state"], "present")

    def test_cli_json_error_missing_dir(self):
        missing = os.path.join(self.tmp, "nope")
        rc, out = _capture_stdout(cli_main,
                                  ["--input-dir", missing, "--json"])
        self.assertEqual(rc, EXIT_IO_ERROR)
        parsed = json.loads(out)
        self.assertEqual(parsed["status"], "error")

    def test_cli_text_report_contains_footprint_error(self):
        self._base(log_text=ERROR_LOG)
        rc, out = _capture_stdout(cli_main, ["--input-dir", self.tmp])
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertIn("F1", out)
        self.assertIn("PCB Footprint", out)
        self.assertIn("ORCAP-36002", out)

    def test_cli_text_report_contains_unsupported_diagnostic(self):
        self._base(log_text=MALFORMED_ERROR_COLON)
        rc, out = _capture_stdout(cli_main, ["--input-dir", self.tmp])
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertIn("unsupported diagnostic lines", out)
        self.assertIn("ERROR: export failed", out)

    def test_cli_new_json_untouched(self):
        self._base(log_text=ERROR_LOG)
        neighbour = os.path.join(self.tmp, "new.json")
        _write(neighbour, b"KEEP-ME-EXACTLY")
        rc, _ = _capture_stdout(cli_main, ["--input-dir", self.tmp])
        self.assertEqual(rc, EXIT_REFUSED)
        with open(neighbour, "rb") as f:
            self.assertEqual(f.read(), b"KEEP-ME-EXACTLY")

    def test_cli_json_is_pure_json(self):
        self._base(log_text=ERROR_LOG)
        rc, out = _capture_stdout(cli_main,
                                  ["--input-dir", self.tmp, "--json"])
        json.loads(out)
        self.assertEqual(rc, EXIT_REFUSED)

    def test_cli_json_byte_identical_on_repeat(self):
        self._base(log_text=ERROR_LOG)
        rc1, out1 = _capture_stdout(cli_main,
                                    ["--input-dir", self.tmp, "--json"])
        rc2, out2 = _capture_stdout(cli_main,
                                    ["--input-dir", self.tmp, "--json"])
        self.assertEqual(rc1, rc2)
        self.assertEqual(out1.encode("utf-8"), out2.encode("utf-8"))


# ---------------------------------------------------------------------------
# Contract with the parser
# ---------------------------------------------------------------------------

class ParserContractTest(unittest.TestCase):

    def test_checker_does_not_import_parser(self):
        import check_capture_export as m
        with open(m.__file__, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("import parse_capture_netlist", src)
        self.assertNotIn("from parse_capture_netlist", src)
        self.assertNotIn("import compare_netlists", src)
        self.assertNotIn("from compare_netlists", src)


if __name__ == "__main__":
    unittest.main()
