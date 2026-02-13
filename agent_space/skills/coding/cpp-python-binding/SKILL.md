---
name: cpp-python-binding
description: Generates and maintains pybind11 bindings for C++ cores. Use when exposing high-performance C++ simulation code to Python, or when debugging interface issues between the two languages.
---

# C++/Python Binding Specialist

You are an expert in `pybind11` and CMake. You bridge the gap between high-performance C++ code (the engine) and flexible Python scripts (the user interface).

## When to use this skill
*   Exposing a new C++ class or function to Python.
*   Debugging type conversion errors (e.g., `TypeError: incompatible function arguments`).
*   Configuring `CMakeLists.txt` to compile standard `.pyd` (Windows) or `.so` (Linux) modules.
*   Mapping Eigen vectors to NumPy arrays.

## Workflow

### 1. Analyze the C++ Source
Identify what needs to be exposed.
*   **Classes:** Need `py::class_<T>`.
*   **Functions:** Need `m.def()`.
*   **Enums:** Need `py::enum_<T>`.

### 2. Implementation Pattern
Create or update the `bindings.cpp` file.

```cpp
#include <pybind11/pybind11.h>
#include <pybind11/eigen.h> // Critical for Vector3d <-> numpy
#include "MyClass.h"

namespace py = pybind11;

PYBIND11_MODULE(nav_bindings, m) {
    m.doc() = "Hoya Box Navigation Bindings";

    py::class_<MyClass>(m, "MyClass")
        .def(py::init<double>(), py::arg("initial_value"))
        .def("update", &MyClass::update)
        .def_readwrite("data", &MyClass::data);
}
```

### 3. CMake Configuration
Ensure the `CMakeLists.txt` is correct:

```cmake
find_package(pybind11 REQUIRED)
pybind11_add_module(nav_bindings bindings.cpp MyClass.cpp)
target_link_libraries(nav_bindings PRIVATE Eigen3::Eigen)
```

### 4. Type Safety Rules
*   **Eigen:** Always include `<pybind11/eigen.h>`.
*   **STL Containers:** Include `<pybind11/stl.h>` if passing `std::vector` or `std::map`.
*   **Pointers:** Be careful with ownership. Use `py::return_value_policy::reference` if Python shouldn't delete the object.

## Troubleshooting
*   **"Symbol not found":** You likely missed adding a `.cpp` source file to the `pybind11_add_module` command in CMake.
*   **"ImportError":** Ensure the compiled output (e.g., `nav_bindings.cp39-win_amd64.pyd`) is in the same directory as the Python script, or added to `sys.path`.

## Best Practices
*   **Keep it Thin:** Do not write complex logic in the binding. It should just forward calls.
*   **Docstrings:** Provide docstrings in the `m.def()` calls so Python IDEs show help text.

