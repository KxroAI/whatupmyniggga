import discord
from discord import app_commands

def setup(bot):
    @bot.tree.command(name="calculator", description="Perform basic math operations")
    @app_commands.describe(num1="First number", operation="Operation to perform", num2="Second number")
    @app_commands.choices(operation=[
        app_commands.Choice(name="Addition (+)", value="add"),
        app_commands.Choice(name="Subtraction (-)", value="subtract"),
        app_commands.Choice(name="Multiplication (*)", value="multiply"),
        app_commands.Choice(name="Division (/)", value="divide")
    ])
    async def calculator(interaction: discord.Interaction, num1: float, operation: app_commands.Choice[str], num2: float):
        if operation.value == "divide" and num2 == 0:
            await interaction.response.send_message("❌ Cannot divide by zero.")
            return
        try:
            if operation.value == "add":
                result = num1 + num2
                symbol = "+"
            elif operation.value == "subtract":
                result = num1 - num2
                symbol = "-"
            elif operation.value == "multiply":
                result = num1 * num2
                symbol = "*"
            elif operation.value == "divide":
                result = num1 / num2
                symbol = "/"
            await interaction.response.send_message(f"Result: `{num1} {symbol} {num2} = {result}`")
        except Exception as e:
            await interaction.response.send_message(f"⚠️ An error occurred: {str(e)}")
