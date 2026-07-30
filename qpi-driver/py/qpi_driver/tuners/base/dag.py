from collections import defaultdict, deque
from typing import Any

from .config import CalibrationConfig
from .report import CalibrationReport, RoutineResult
from .routines import CalibrationRoutine


class CalibrationDAG:
    def __init__(
        self, routines: list[CalibrationRoutine], config: CalibrationConfig
    ) -> None:
        self.routines = {r.name: r for r in routines}
        self.config = config

    def execution_order(self) -> list[str]:
        """Kahn's algorithm topological sort."""
        in_degree = {name: 0 for name in self.routines}
        adj_list = defaultdict(list)

        for name, routine in self.routines.items():
            for dep in routine.depends_on:
                if dep in self.routines:
                    adj_list[dep].append(name)
                    in_degree[name] += 1

        queue = deque([name for name, deg in in_degree.items() if deg == 0])
        order = []

        while queue:
            node = queue.popleft()
            if self.config.is_enabled(node):
                order.append(node)

            for neighbor in adj_list[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(order) < sum(1 for n in self.routines if self.config.is_enabled(n)):
            raise ValueError("Cycle detected in calibration dependencies")

        return order

    def partial_order(self, targets: list[str]) -> list[str]:
        """Subgraph for partial recalibration.

        Args:
            targets: List of routine names to recalibrate and their dependents.
        """
        all_order = self.execution_order()

        adj_list = defaultdict(list)
        for name, routine in self.routines.items():
            for dep in routine.depends_on:
                if dep in self.routines:
                    adj_list[dep].append(name)

        visited = set()
        queue = deque(targets)

        while queue:
            node = queue.popleft()
            if node not in visited:
                visited.add(node)
                queue.extend(adj_list[node])

        return [r for r in all_order if r in visited]

    def run(
        self, device: Any, instrument_coordinator: Any, compiler: Any, config: Any
    ) -> CalibrationReport:
        import time
        from datetime import datetime

        report = CalibrationReport(
            timestamp=datetime.now().isoformat(), duration_s=0.0, mode="full"
        )
        start_time = time.time()

        try:
            for routine_name in self.execution_order():
                routine = self.routines[routine_name]
                routine_config = self.config.get_routine(routine_name)

                targets = (
                    self.config.target_qubits
                    if routine.targets == "qubits"
                    else self.config.target_edges
                )

                for target in targets:
                    routine_start = time.time()

                    schedule = routine.build_schedule(target, device, routine_config)
                    compiled_schedule = compiler.compile(schedule)
                    instrument_coordinator.prepare(compiled_schedule)
                    instrument_coordinator.start()
                    dataset = instrument_coordinator.retrieve_acquisition()

                    params = routine.analyse(dataset, target, device)
                    routine.apply(device, target, params)

                    routine_duration = time.time() - routine_start

                    report.add_routine(
                        RoutineResult(
                            routine_name=routine_name,
                            target=target,
                            parameters=params,
                            timestamp=datetime.now().isoformat(),
                            duration_s=routine_duration,
                        )
                    )
        except Exception as e:
            report.status = "failed"
            report.errors.append(str(e))

        report.duration_s = time.time() - start_time
        return report
