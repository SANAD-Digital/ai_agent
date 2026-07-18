class BaseProvider:
    """Base class for model providers."""

    def create_step(self, *, instructions, input_items, tools, state):
        raise NotImplementedError

    def continue_step(self, *, response_id, tool_outputs, tools, state):
        raise NotImplementedError

    def get_usage(self, response):
        raise NotImplementedError
