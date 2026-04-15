"""Tests for pulsar.collector — hardware data collection."""

import pytest
import psutil

from pulsar.collector import collect, get_system_info, Snapshot, SystemInfo


class TestSystemInfo:
    def test_returns_system_info(self):
        info = get_system_info()
        assert isinstance(info, SystemInfo)

    def test_cpu_cores_positive(self):
        info = get_system_info()
        assert info.cpu_cores >= 1

    def test_ram_total_positive(self):
        info = get_system_info()
        assert info.ram_total > 0

    def test_cpu_model_nonempty(self):
        info = get_system_info()
        assert isinstance(info.cpu_model, str)
        assert len(info.cpu_model) > 0


class TestCollect:
    def test_returns_snapshot(self):
        snap = collect()
        assert isinstance(snap, Snapshot)

    def test_cpu_per_core_length_matches_logical_cores(self):
        snap = collect()
        assert len(snap.cpu_per_core) == psutil.cpu_count(logical=True)

    def test_cpu_values_in_range(self):
        snap = collect()
        for v in snap.cpu_per_core:
            assert 0.0 <= v <= 100.0

    def test_mem_total_positive(self):
        snap = collect()
        assert snap.mem_total > 0
        assert snap.mem_used >= 0

    def test_mem_percent_in_range(self):
        snap = collect()
        assert 0.0 <= snap.mem_percent <= 100.0

    def test_disk_mbps_non_negative(self):
        snap = collect()
        assert snap.disk_read_mbps >= 0
        assert snap.disk_write_mbps >= 0

    def test_top_procs_default_limit(self):
        snap = collect(top_n=5)
        assert len(snap.top_procs) <= 5

    def test_top_procs_custom_limit(self):
        snap = collect(top_n=3)
        assert len(snap.top_procs) <= 3

    def test_proc_fields_present(self):
        snap = collect(top_n=3)
        for proc in snap.top_procs:
            assert "pid" in proc
            assert "name" in proc
            assert "cpu" in proc
            assert "mem_mb" in proc

    def test_proc_filter_returns_subset(self):
        # Use a filter that matches nothing — result should be empty
        snap = collect(proc_filter=["__no_such_process_xyz__"])
        assert snap.top_procs == []
