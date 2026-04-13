import json
import os
import pytest
from unittest import mock
import sys
from pathlib import Path

# Add the project root to the python path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.epic_manager_tool import EpicManager, epic_delegate, CycleError

class TestEpicManager:
    @pytest.fixture
    def manager(self):
        return EpicManager(verbose=False)

    def test_topological_sort_success(self, manager):
        tasks = [
            {"id": "t1", "title": "Setup", "depends_on": []},
            {"id": "t2", "title": "DB", "depends_on": ["t1"]},
            {"id": "t3", "title": "API", "depends_on": ["t2"]},
            {"id": "t4", "title": "UI", "depends_on": ["t3", "t1"]},
        ]
        
        sorted_tasks = manager._topological_sort(tasks)
        
        # Verify length
        assert len(sorted_tasks) == 4
        
        # Verify order
        output_ids = [t["id"] for t in sorted_tasks]
        assert output_ids.index("t1") < output_ids.index("t2")
        assert output_ids.index("t2") < output_ids.index("t3")
        assert output_ids.index("t3") < output_ids.index("t4")

    def test_topological_sort_cycle_error(self, manager):
        tasks = [
            {"id": "t1", "title": "Setup", "depends_on": ["t3"]},
            {"id": "t2", "title": "DB", "depends_on": ["t1"]},
            {"id": "t3", "title": "API", "depends_on": ["t2"]}
        ]
        
        with pytest.raises(CycleError):
            manager._topological_sort(tasks)

    @mock.patch("tools.epic_manager_tool.delegate_task")
    def test_generate_task_graph_valid_json(self, mock_delegate, manager):
        # Mocking the LLM raw string return
        json_str = '''
        ```json
        [
            {"id": "t1", "title": "A", "depends_on": []},
            {"id": "t2", "title": "B", "depends_on": ["t1"]}
        ]
        ```
        '''
        mock_delegate.return_value = json.dumps({"results": [{"output": json_str}]})
        
        tasks = manager._generate_task_graph("Test Epic")
        assert len(tasks) == 2
        assert tasks[0]["id"] == "t1"
        assert tasks[1]["id"] == "t2"

    @mock.patch("tools.epic_manager_tool.delegate_task")
    def test_generate_task_graph_invalid_json(self, mock_delegate, manager):
        # LLM returns garbage
        mock_delegate.return_value = json.dumps({"results": [{"output": "Here are your tasks, I couldn't format them."}]})
        
        tasks = manager._generate_task_graph("Test Epic")
        assert isinstance(tasks, list)
        assert len(tasks) == 0

    @mock.patch("tools.epic_manager_tool.subprocess.run")
    @mock.patch("tools.epic_manager_tool.staged_delegate")
    def test_execute_graph_sequential_execution(self, mock_staged, mock_run, manager):
        tasks = [
            {"id": "t1", "title": "Setup", "description": "do setup", "depends_on": [], "verification_command": "echo 'ok'"},
            {"id": "t2", "title": "DB", "description": "do db", "depends_on": ["t1"]}
        ]
        
        # Mock staged_delegate returning success JSON
        mock_staged.return_value = json.dumps({"success": True, "iterations": 2})
        # Mock subprocess returning success (0)
        mock_proc = mock.Mock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc
        
        res = manager._execute_graph("Build App", tasks)
        
        assert res["success"] is True
        assert res["total_tasks"] == 2
        assert res["completed_tasks"] == 2
        assert mock_staged.call_count == 2
        assert mock_run.call_count == 1 # Only t1 has a verification_command
        mock_run.assert_called_with("echo 'ok'", shell=True, capture_output=True, text=True, cwd=mock.ANY)
        
        # First call args check
        call1_args = mock_staged.call_args_list[0][1]
        assert "REQUIRED VERIFICATION COMMAND: echo 'ok'" in call1_args["goal"]

    @mock.patch("tools.epic_manager_tool.subprocess.run")
    @mock.patch("tools.epic_manager_tool.staged_delegate")
    def test_execute_graph_quality_gate_failure(self, mock_staged, mock_run, manager):
        tasks = [
            {"id": "t1", "title": "Setup", "depends_on": [], "verification_command": "pytest test.py"},
            {"id": "t2", "title": "DB", "depends_on": ["t1"]}
        ]
        
        # Agent says success
        mock_staged.return_value = json.dumps({"success": True, "iterations": 2})
        
        # But Physical Quality Gate fails
        mock_proc = mock.Mock()
        mock_proc.returncode = 1
        mock_proc.stdout = "Failed test"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc
        
        res = manager._execute_graph("Build App", tasks)
        
        assert res["success"] is False
        assert res["completed_tasks"] == 0 # First task fails due to Q-Gate
        assert mock_staged.call_count == 1 # Second task shouldn't run
        mock_run.assert_called_once()

    def test_save_and_load_plan(self, manager, tmp_path):
        tasks = [{"id": "t1", "title": "Test"}]
        test_file = tmp_path / "epic_plan_test.json"
        
        # Save
        assert manager.save_plan(tasks, str(test_file)) is True
        assert test_file.exists()
        
        # Load
        loaded_tasks = manager.load_plan(str(test_file))
        assert len(loaded_tasks) == 1
        assert loaded_tasks[0]["id"] == "t1"
        
        # Load non-existent
        assert manager.load_plan("does_not_exist.json") == []

    @mock.patch("tools.epic_manager_tool.EpicManager._generate_storyboard_elements")
    @mock.patch("tools.epic_manager_tool.EpicManager.save_plan", return_value=True)
    @mock.patch("tools.epic_manager_tool.EpicManager._generate_task_graph")
    def test_epic_delegate_plan_only(self, mock_gen, mock_save, mock_storyboard):
        mock_gen.return_value = [{"id": "t1", "title": "Test"}]
        mock_storyboard.return_value = {
            "mermaid_chart": "graph TD; A-->B;",
            "html_prototype": "<html>fake</html>",
            "image_generation_prompt": ""
        }
        
        result_str = epic_delegate("Make an app", mode="plan_only")
        result = json.loads(result_str)
        
        assert result["success"] is True
        assert "t1" in result["message"]
        assert "graph TD;" in result["message"]
        assert "epic_wireframe_prototype.html" in result["message"]
        mock_save.assert_called_once()
        mock_gen.assert_called_once()

    @mock.patch("tools.epic_manager_tool.EpicManager._execute_graph", return_value={"success": True, "mode": "epic"})
    @mock.patch("tools.epic_manager_tool.EpicManager.load_plan")
    def test_epic_delegate_execute(self, mock_load, mock_exec):
        mock_load.return_value = [{"id": "t1"}]
        result_str = epic_delegate("Make an app", mode="execute")
        result = json.loads(result_str)
        
        assert result["success"] is True
        mock_load.assert_called_once()
        mock_exec.assert_called_once()

    def test_generate_workspace_tree(self, manager, tmp_path):
        import os
        # Create a mock project structure
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "api").mkdir()
        (tmp_path / "src" / "api" / "routes").mkdir()
        
        (tmp_path / "src" / "main.py").write_text("print('hello')")
        (tmp_path / "src" / "api" / "auth.py").write_text("auth func")
        (tmp_path / "src" / "api" / "routes" / "user.py").write_text("user route")
        
        # Ignored dir
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "test.js").write_text("js")
        
        tree_str = manager._generate_workspace_tree(str(tmp_path), max_depth=2)
        
        # Should contain structural icons
        assert "📂" in tree_str
        assert "📄" in tree_str
        
        # depth 2 should see main.py and auth.py, but maybe not user.py (depth 3)
        # root is depth 0
        # src is depth 1
        # main.py is depth 1 file
        # src/api is depth 2
        # auth.py is depth 2 file
        # src/api/routes is depth 3 -> max_depth=2 stops before showing its files
        
        assert "main.py" in tree_str
        assert "auth.py" in tree_str
        assert "user.py" not in tree_str
        
        # Ignored dirs
        assert "node_modules" not in tree_str

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
